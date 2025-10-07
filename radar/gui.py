"""
radar.gui
=========

Mini-Radar GUI – MQTT / Serial input, contextual CONFIG, data-loss handling

Key features
------------
• Pulse rings & colour-coded fading trails
• Rolling-average smoothing (LOW 3 frames → HIGH 50 frames)
• SOUND / NIGHT / TRAIL / SMOOTH toggles
• Two-step “Set Sensor” wizard
• Contextual CONFIG dialog that hides irrelevant fields
• Live clock (bottom-right)
• Flashing red “DATA STREAM LOST” banner when no frames arrive for ≥1 s;
  live tracks are immediately closed and archived.
"""

from __future__ import annotations
import math, time, datetime as dt, collections, pygame, pygame.cursors
from typing import List, Tuple, Union

from radar import constants as C
from radar.svg_utils import fit_svg
from radar.mqtt_client import RadarMQTT
from radar.serial_reader import RadarSerial
from radar.tracking import Tracker
from radar.sound import beep


class RadarGUI:
    MAX_V, FOV_DEG   = 4000, 120        # speed cap for colour / beep; radar FOV°
    DATA_TIMEOUT_SEC = 1.0              # gap that triggers DATA-LOSS banner

    # ────────────────────────────────────────────────── INIT
    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg

        # ―― Pygame window
        self.screen = pygame.display.set_mode((1100, 750), pygame.RESIZABLE)
        pygame.display.set_caption("Mini-Radar")
        self.clock = pygame.time.Clock()

        # ―― Layout & sensor pose
        self.full_screen = False
        self.map_mode    = False
        self.playback_mode = False
        self.top_pad, self.bottom_pad = C.TOP_PAD_N, C.BOTTOM_PAD_N
        self.svg_surf = None
        self.ppm = 1.0; self.off_x = self.off_y = 0
        self.sensor_mm = cfg["sensor"][:]       # [x_mm, y_mm]
        self.sensor_hd = cfg["heading"]         # degrees ±180

        # ―― Toggles
        self.sound_on   = cfg["sound"]
        self.night_mode = cfg["night"]
        self.trail_on   = bool(cfg.get("trail_on", True))

        # ―― Smoothing
        self.smoothing_on = bool(cfg.get("smoothing_on", True))
        self.smooth_level = int(cfg.get("smooth_level", 0))   # 0–9

        # ―― Trails & motion history
        self.trail_duration = float(cfg.get("trail_duration", 5.0))
        self.trails:  dict[str, collections.deque] = {}       # ser → deque[(px,py,t,col)]
        self.motion_hist: dict[str, collections.deque] = {}   # ser → deque[(x,y,v)]
        self.pulse_phase: dict[str, float] = {}

        # ―― Interaction & wizard
        self.sensor_stage = None          # None | 'intro' | 'placing' | 'heading'
        self.placing_sensor = self.rotating_sensor = False
        self.drag_slider = self.drag_level = False
        self.knob_rect = self.level_rect = pygame.Rect(0,0,0,0)
        self.heading_btn_rect = pygame.Rect(0,0,0,0)
        self.menu_rects, self.exit_rect = {}, pygame.Rect(0,0,0,0)
        pygame.mouse.set_cursor(*pygame.cursors.arrow)

        # ―― Input mode & reader
        self.input_mode  = cfg.get("input_mode", "mqtt").lower()
        self.serial_port = cfg.get("serial_port", "/dev/ttyUSB0")
        self.serial_baud = int(cfg.get("serial_baud", 256000))

        self.tracker = Tracker()
        self.latest: List[Tuple[int,int,int]] = []
        self.fastest = 0.0
        self.reader: Union[RadarMQTT, RadarSerial]
        self._open_input()

        # ―― Timers & watchdog
        self.flash=True; self.t_flash=time.monotonic()
        self._last_beep = 0.0
        self.t_last_frame = time.monotonic()
        self.data_lost = False

        # ―― CONFIG dialog
        self.show_cfg=False
        self.fields = ["Broker IP", "Port", "Topic",
                       "Serial Port", "Trail Duration", "Trail ON"]
        self.cfg_input = [str(cfg["broker"]), str(cfg["port"]), cfg["topic"],
                          self.serial_port, str(self.trail_duration), str(self.trail_on)]
        self.visible_idx: List[int] = []
        self.cur_vis = self.cur_field = 0
        self._update_visible()
        self.cfg_buttons = {}

        self.refresh_map()


    # ───────────────────────────────────────── launch playback viewer
    def _launch_playback(self, preset_path: str | None = None):
        """
        ①  Pause the live reader  
        ②  Start radar.playback_gui.RadarPlaybackGUI  
            • If preset_path is None the selection screen appears  
            • If preset_path is a CSV the viewer opens it immediately  
        ③  When the user clicks “Exit”, resume live streaming
        """
        # 1) stop live stream
        if isinstance(self.reader, RadarMQTT):
            self.reader.cli.loop_stop()
        else:
            self.reader.stop()

        # 2) launch playback window (selection UI handles drag-drop, recent list, textbox)
        from radar.playback_gui import RadarPlaybackGUI      # lazy import
        self.playback_mode = True
        RadarPlaybackGUI(self.cfg, preset_path).run()        # blocks here
        self.playback_mode = False

        # 3) resume live stream
        self._open_input()



    # ───────────────────────────────────────── helper – open data source
    def _open_input(self):
        # close previous
        if hasattr(self, "reader"):
            if isinstance(self.reader, RadarMQTT):
                self.reader.cli.loop_stop()
            else:
                self.reader.stop()
        # open new
        if self.input_mode == "mqtt":
            self.reader = RadarMQTT(self.cfg["broker"], self.cfg["port"],
                                    self.cfg["topic"], self._on_frame)
            self.reader.connect()
        else:
            self.reader = RadarSerial(self.serial_port, self.serial_baud,
                                      self._on_frame)
            self.reader.start()

    # ───────────────────────────────────────── helper – visible CFG fields
    def _update_visible(self):
        self.visible_idx = [0,1,2,4,5] if self.input_mode == "mqtt" else [3,4,5]
        self.cur_vis = min(self.cur_vis, len(self.visible_idx)-1)
        self.cur_field = self.visible_idx[self.cur_vis]

    # ───────────────────────────────────────── smoothing window (3→50)
    def _win(self) -> int:
        return 1 if not self.smoothing_on else 3 + round(self.smooth_level * 47 / 9)

    # ───────────────────────────────────────── sync runtime→cfg
    def _sync_cfg(self):
        self.cfg.update(sensor=self.sensor_mm, heading=self.sensor_hd,
                        sound=self.sound_on, night=self.night_mode,
                        trail_duration=self.trail_duration, trail_on=self.trail_on,
                        smoothing_on=self.smoothing_on, smooth_level=self.smooth_level)

    # ───────────────────────────────────────── frame callback
    def _on_frame(self, lst):
        """
        lst comes from RadarMQTT / RadarSerial and may contain
        3-tuples  (slot,x,y)            – legacy
        4-tuples  (slot,x,y,raw_hex)    – new

        We pass the full list to Tracker so it can log raw_hex, but
        keep a trimmed 3-tuple version for GUI drawing.
        """
        self.fastest = self.tracker.update(lst)     # needs full tuples
        self.latest  = [t[:3] for t in lst]         # (slot,x,y) only
        self.t_last_frame = time.monotonic()        # reset watchdog
        self.data_lost    = False                   # banner off

    # ───────────────────────────────────────── helper – close all live tracks
    def _end_all_targets(self):
        """Archive every active track & clear all live state."""
        now = dt.datetime.now()
        for ser, info in list(self.tracker.active.items()):
            record = {
                "serial": ser,
                "first":  info["first"],
                "last":   now,
                "dur":    now - info["first"],
            }
            self.tracker.recent.append(record)
            # clean up
            del self.tracker.active[ser]
            self.trails.pop(ser, None)
            self.motion_hist.pop(ser, None)
            self.pulse_phase.pop(ser, None)

        # Purge dead serials from slot map
        self.tracker.slot2ser = {s: t for s, t in self.tracker.slot2ser.items()
                                 if t in self.tracker.active}

        self.latest.clear()

    # ───────────────────────────────────────── geometry helpers
    def mm_to_px(self, mx, my):
        return (self.off_x + int(mx * self.ppm),
                self.off_y + int(self.svg_surf.get_height() - my * self.ppm))
    def px_to_mm(self, px, py):
        return ((px - self.off_x) / self.ppm,
                (self.svg_surf.get_height() - (py - self.off_y)) / self.ppm)
    def local_to_world(self, xl, yl):
        sx, sy = self.sensor_mm
        c, s = math.cos(math.radians(self.sensor_hd)), math.sin(math.radians(self.sensor_hd))
        return sx + xl * c + yl * s, sy - xl * s + yl * c

    # ───────────────────────────────────────── SVG raster
    def refresh_map(self):
        if self.map_mode:
            box = (self.screen.get_width() - 2 * C.MAP_BORDER,
                   self.screen.get_height() - 2 * C.MAP_BORDER)
            self.svg_surf, self.ppm = fit_svg(self.cfg["map"], box)
            self.off_x = C.MAP_BORDER + (box[0] - self.svg_surf.get_width()) // 2
            self.off_y = C.MAP_BORDER
        else:
            box = (self.screen.get_width(),
                   self.screen.get_height() - self.top_pad - self.bottom_pad)
            self.svg_surf, self.ppm = fit_svg(self.cfg["map"], box)
            self.off_x = (box[0] - self.svg_surf.get_width()) // 2
            self.off_y = self.top_pad + (box[1] - self.svg_surf.get_height()) // 2

    # ───────────────────────────────────────── menu row
    def _menu_row(self):
        r,x,y={},self.screen.get_width()-10,10
        def add(label,key,col=C.GREEN):
            nonlocal x
            surf=C.FONT.render(label,True,col); rr=surf.get_rect(); rr.topright=(x,y)
            self.screen.blit(surf,rr); r[key]=rr; x=rr.left-20
        add("EXIT_FULL" if self.full_screen else "FULL_SCREEN","full")
        if not self.map_mode: add("MAP","map")
        add("CONFIG","config")
        add("TRAIL","trail",C.GREEN if self.trail_on else C.DIM)
        add("SMOOTH","smooth",C.GREEN if self.smoothing_on else C.DIM)
        add("NIGHT","night",C.RED if self.night_mode else C.GREEN)
        add("SOUND","sound",C.GREEN if self.sound_on else C.DIM)
        add("PLAYBACK","playback",C.GREEN if self.playback_mode else C.DIM)
        r["h"]=C.FONT.get_height(); self.menu_rects=r

    # ───────────────────────────────────────── rotation slider
    def _draw_slider(self, angle):
        slid = pygame.Rect(0,0,220,12)
        slid.midbottom = (self.screen.get_width()//2,
                          self.screen.get_height()-30)
        pygame.draw.rect(self.screen, C.DIM, slid, 1)
        knobx = slid.left + int((angle+180)/360 * slid.width)
        knob = pygame.Rect(0,0,8,20); knob.midbottom=(knobx,slid.bottom)
        pygame.draw.rect(self.screen,C.GREEN,knob)
        self.knob_rect = knob
        self.screen.blit(C.FONT.render(f"{angle:+.0f}°",True,C.GREEN),
                         (slid.right+8,slid.top-6))
        return slid

    # ───────────────────────────────────────── CONFIG pop-up
    def _draw_cfg_popup(self):
        w,h = 600,420
        rect = pygame.Rect((self.screen.get_width()-w)//2,
                           (self.screen.get_height()-h)//2,w,h)
        pygame.draw.rect(self.screen,C.BLACK,rect); pygame.draw.rect(self.screen,C.GREEN,rect,2)

        # input-mode buttons
        self.screen.blit(C.MID_FONT.render("Input Mode:",True,C.GREEN),
                         (rect.x+20,rect.y+20))
        mqtt_r = pygame.Rect(rect.x+220,rect.y+15,90,30)
        ser_r  = pygame.Rect(mqtt_r.right+20,rect.y+15,90,30)
        for r,lbl,on in [(mqtt_r,"MQTT",self.input_mode=="mqtt"),
                         (ser_r,"SERIAL",self.input_mode=="serial")]:
            pygame.draw.rect(self.screen,C.GREEN if on else C.DIM,r,2)
            self.screen.blit(C.FONT.render(lbl,True,C.GREEN),
                             C.FONT.render(lbl,True,C.GREEN).get_rect(center=r.center))
        self.cfg_buttons.update({"mode_mqtt":mqtt_r,"mode_serial":ser_r})

        # fields
        start_y = rect.y+70
        for vis_i,f in enumerate(self.visible_idx):
            txt = f"{self.fields[f]}: {self.cfg_input[f]}" + (" ▌" if f==self.cur_field else "")
            surf = C.MID_FONT.render(txt,True,C.GREEN if f==self.cur_field else C.DIM)
            self.screen.blit(surf,(rect.x+20,start_y+vis_i*45))

        # smoothing slider
        slide = pygame.Rect(rect.x+150,rect.bottom-160,300,12)
        pygame.draw.rect(self.screen,C.DIM,slide,1)
        for t in range(11):
            tx = slide.left+int(t*slide.width/10)
            pygame.draw.line(self.screen,C.DIM,(tx,slide.bottom),(tx,slide.bottom+4))
        knobx = slide.left+int(self.smooth_level*slide.width/9)
        k = pygame.Rect(0,0,10,18); k.midbottom=(knobx,slide.bottom)
        pygame.draw.rect(self.screen,C.GREEN,k); self.level_rect=slide
        label=C.SMALL_FONT.render("Target Smoothing:",True,C.GREEN)
        self.screen.blit(label,(slide.centerx-label.get_width()//2,slide.top-20))
        self.screen.blit(C.SMALL_FONT.render("LOW",True,C.GREEN),
                         (slide.left-35,slide.top-5))
        self.screen.blit(C.SMALL_FONT.render("HIGH",True,C.GREEN),
                         (slide.right+5,slide.top-5))

        # buttons
        ss = pygame.Rect(rect.left+20,rect.bottom-90,140,35)
        save = pygame.Rect(rect.right-190,rect.bottom-50,80,35)
        cancel = pygame.Rect(rect.right-100,rect.bottom-50,80,35)
        for b,lbl in [(ss,"SET SENSOR"),(save,"SAVE"),(cancel,"CANCEL")]:
            pygame.draw.rect(self.screen,C.GREEN,b,2)
            self.screen.blit(C.FONT.render(lbl,True,C.GREEN),
                             C.FONT.render(lbl,True,C.GREEN).get_rect(center=b.center))
        self.cfg_buttons.update({"save":save,"cancel":cancel,"set_sensor":ss})

    # ───────────────────────────────────────── wizard intro overlay
    def _draw_sensor_intro(self):
        w,h=520,230
        r=pygame.Rect((self.screen.get_width()-w)//2,
                      (self.screen.get_height()-h)//2,w,h)
        pygame.draw.rect(self.screen,C.BLACK,r); pygame.draw.rect(self.screen,C.GREEN,r,2)
        for i,line in enumerate(("SET SENSOR",
                                 "1) Click map where sensor is located",
                                 "2) Rotate slider to set heading")):
            self.screen.blit(C.MID_FONT.render(line,True,C.GREEN),
                             (r.x+20,r.y+25+i*45))
        setb=pygame.Rect(r.x+40,r.bottom-60,180,40)
        canc=pygame.Rect(r.right-180,r.bottom-60,120,40)
        pygame.draw.rect(self.screen,C.GREEN,setb,2)
        pygame.draw.rect(self.screen,C.GREEN,canc,2)
        self.screen.blit(C.FONT.render("SET POSITION",True,C.GREEN),setb.move(10,8))
        self.screen.blit(C.FONT.render("CANCEL",True,C.GREEN),canc.move(25,8))
        self.sensor_buttons={"set":setb,"cancel":canc}

    # ───────────────────────────────────────── heading finish button
    def _draw_heading_btn(self,slid_rect):
        btn=pygame.Rect(slid_rect.right+30,slid_rect.top-6,140,30)
        pygame.draw.rect(self.screen,C.GREEN,btn,2)
        self.screen.blit(C.FONT.render("SET HEADING",True,C.GREEN),btn.move(8,5))
        self.heading_btn_rect=btn

    # ───────────────────────────────────────── save CONFIG
    def _cfg_save(self):
        self.cfg.update(
            input_mode=self.input_mode,
            broker=self.cfg_input[0],
            port=int(self.cfg_input[1] or 1883),
            topic=self.cfg_input[2],
            serial_port=self.cfg_input[3],
            trail_duration=float(self.cfg_input[4] or 5.0),
            trail_on=self.cfg_input[5].strip().lower() in ("true","1","yes","on"),
            smoothing_on=self.smoothing_on,
            smooth_level=self.smooth_level)
        self.serial_port   = self.cfg["serial_port"]
        self.trail_duration = float(self.cfg["trail_duration"])
        self.trail_on       = bool(self.cfg["trail_on"])
        self._open_input()
        self._sync_cfg()

    # ───────────────────────────────────────── avg motion helper
    def _avg_motion(self,ser,x,y,v):
        win=self._win()
        hist=self.motion_hist.setdefault(ser,collections.deque(maxlen=win))
        if hist.maxlen!=win:
            hist=collections.deque(hist,maxlen=win); self.motion_hist[ser]=hist
        hist.append((x,y,v))
        ax=sum(h[0] for h in hist)/len(hist)
        ay=sum(h[1] for h in hist)/len(hist)
        av=sum(h[2] for h in hist)/len(hist)
        return ax,ay,av

    # ───────────────────────────────────────── MAIN LOOP
    def run(self):
        running=True
        while running:
            dt_frame=self.clock.tick(30)/1000
            if time.monotonic()-self.t_flash>0.5:
                self.flash=not self.flash; self.t_flash=time.monotonic()

            # ――― EVENTS ―――――――――――――――――――――――――――――――――――――――――――
            for e in pygame.event.get():
                if e.type==pygame.QUIT:
                    running=False

                elif e.type==pygame.VIDEORESIZE and not self.full_screen:
                    self.screen=pygame.display.set_mode(e.size,pygame.RESIZABLE)
                    self.refresh_map()

                # Wizard intro buttons
                if self.sensor_stage=="intro" and e.type==pygame.MOUSEBUTTONDOWN and e.button==1:
                    if self.sensor_buttons["cancel"].collidepoint(e.pos):
                        self.sensor_stage=None
                    elif self.sensor_buttons["set"].collidepoint(e.pos):
                        self.sensor_stage="placing"; self.placing_sensor=True
                        pygame.mouse.set_cursor(*pygame.cursors.broken_x)
                    continue

                # Heading finish
                if self.sensor_stage=="heading" and e.type==pygame.MOUSEBUTTONDOWN and e.button==1:
                    if self.heading_btn_rect.collidepoint(e.pos):
                        self.rotating_sensor=False; self.sensor_stage=None; self._sync_cfg()
                        continue

                # CONFIG dialog events
                if self.show_cfg:
                    if "save" not in self.cfg_buttons: continue
                    if e.type==pygame.KEYDOWN:
                        if e.key==pygame.K_ESCAPE:
                            self.show_cfg=False
                        elif e.key in (pygame.K_TAB,pygame.K_RETURN):
                            self.cur_vis=(self.cur_vis+1)%len(self.visible_idx)
                            self.cur_field=self.visible_idx[self.cur_vis]
                        elif e.key==pygame.K_BACKSPACE:
                            self.cfg_input[self.cur_field]=self.cfg_input[self.cur_field][:-1]
                        elif e.unicode and 32<=ord(e.unicode)<127:
                            self.cfg_input[self.cur_field]+=e.unicode
                    elif e.type==pygame.MOUSEBUTTONDOWN and e.button==1:
                        if self.cfg_buttons["save"].collidepoint(e.pos):
                            self._cfg_save(); self.show_cfg=False
                        elif self.cfg_buttons["cancel"].collidepoint(e.pos):
                            self.show_cfg=False
                        elif self.cfg_buttons["set_sensor"].collidepoint(e.pos):
                            self.show_cfg=False; self.sensor_stage="intro"
                        elif self.level_rect.collidepoint(e.pos):
                            self.drag_level=True
                        elif self.cfg_buttons["mode_mqtt"].collidepoint(e.pos):
                            self.input_mode="mqtt"; self._update_visible()
                        elif self.cfg_buttons["mode_serial"].collidepoint(e.pos):
                            self.input_mode="serial"; self._update_visible()
                    elif e.type==pygame.MOUSEBUTTONUP and e.button==1:
                        self.drag_level=False
                    elif e.type==pygame.MOUSEMOTION and self.drag_level:
                        pos=e.pos[0]; left,right=self.level_rect.left,self.level_rect.right
                        pos=max(left,min(right,pos))
                        self.smooth_level=round((pos-left)*9/(right-left))
                        self._sync_cfg()
                    continue  # dialog eats events

                # Hot-keys (no dialog - These are keys that can be pressed by the user to call menu or program actions)
                if self.sensor_stage is None and e.type==pygame.KEYDOWN:
                    # Escape Key will quit the running program
                    if e.key in (pygame.K_q,pygame.K_ESCAPE):
                        running=False
                    # 'f'-key will toggle the program in and out of full-screen or window mode    
                    elif e.key==pygame.K_f:
                        pygame.display.toggle_fullscreen()
                        self.full_screen=not self.full_screen; self.refresh_map()
                    # 'n'-key will toggle "Night Mode" on and off  
                    elif e.key==pygame.K_n:
                        self.night_mode=not self.night_mode; self._sync_cfg()
                    # 's'-key will toggle "Sound" on and off  
                    elif e.key==pygame.K_s:
                        self.sound_on=not self.sound_on; self._sync_cfg()
                    # 'p'-key will toggle the playback mode
                    elif e.key==pygame.K_p:
                        self._launch_playback()

                # Menu clicks & general mouse
                if e.type==pygame.MOUSEBUTTONDOWN and e.button==1:
                    # map exit
                    if self.map_mode and self.exit_rect.collidepoint(e.pos):
                        self.map_mode=False
                        self.top_pad=C.TOP_PAD_N; self.bottom_pad=C.BOTTOM_PAD_N
                        self.refresh_map(); continue

                    m=self.menu_rects
                    if self.sensor_stage is None and m:
                        if not self.map_mode and m["full"].collidepoint(e.pos):
                            pygame.display.toggle_fullscreen()
                            self.full_screen=not self.full_screen; self.refresh_map(); continue
                        if not self.map_mode and "map" in m and m["map"].collidepoint(e.pos):
                            self.map_mode=True; self.top_pad=self.bottom_pad=C.MAP_BORDER
                            self.refresh_map(); continue
                        if not self.map_mode and m["config"].collidepoint(e.pos):
                            self.show_cfg=True; self.cur_vis=0; self.cur_field=self.visible_idx[0]; continue
                        if not self.map_mode and m["trail"].collidepoint(e.pos):
                            self.trail_on=not self.trail_on; self._sync_cfg(); continue
                        if not self.map_mode and m["smooth"].collidepoint(e.pos):
                            self.smoothing_on=not self.smoothing_on; self._sync_cfg(); continue
                        if not self.map_mode and m["night"].collidepoint(e.pos):
                            self.night_mode=not self.night_mode; self._sync_cfg(); continue
                        if not self.map_mode and m["sound"].collidepoint(e.pos):
                            self.sound_on=not self.sound_on; self._sync_cfg(); continue
                        if not self.map_mode and m["playback"].collidepoint(e.pos):
                                self._launch_playback()
                                continue                            

                    # placing click
                    if self.sensor_stage=="placing":
                        self.sensor_mm[:]=self.px_to_mm(*e.pos)
                        self.placing_sensor=False; self.rotating_sensor=True
                        self.sensor_stage="heading"; pygame.mouse.set_cursor(*pygame.cursors.arrow)
                        continue

                    # heading knob drag
                    if self.sensor_stage=="heading" and self.knob_rect.collidepoint(e.pos):
                        self.drag_slider=True; continue

                elif e.type==pygame.MOUSEBUTTONUP and e.button==1:
                    self.drag_slider=False

                elif e.type==pygame.MOUSEMOTION and self.drag_slider:
                    left=(self.screen.get_width()//2)-110; right=left+220
                    x=max(left,min(right,e.pos[0]))
                    self.sensor_hd=((x-left)/220)*360-180

            # ――― STREAM WATCHDOG ―――――――――――――――――――――――――
            if not self.data_lost and (time.monotonic()-self.t_last_frame)>self.DATA_TIMEOUT_SEC:
                self.data_lost=True
                self._end_all_targets()

            # ――― DRAWING ――――――――――――――――――――――――――――――
            self.screen.fill(C.BLACK)
            self.screen.blit(self.svg_surf,(self.off_x,self.off_y))

            # header / menu / clock
            if not self.map_mode:
                y=10
                for surf in C.ASCII_SURFS:
                    self.screen.blit(surf,(10,y)); y+=surf.get_height()
                self._menu_row()
                clk=C.BIG_FONT.render(dt.datetime.now().strftime("%H:%M:%S"),True,C.GREEN)
                self.screen.blit(clk,(self.screen.get_width()-clk.get_width()-10,
                                       self.screen.get_height()-clk.get_height()-10))

            # slider / wizard
            if self.rotating_sensor:
                slid=self._draw_slider(self.sensor_hd)
                if self.sensor_stage=="heading": self._draw_heading_btn(slid)
            elif self.sensor_stage=="placing":
                msg=C.FONT.render("SET POSITION NOW",True,C.GREEN)
                self.screen.blit(msg,(self.screen.get_width()//2-msg.get_width()//2,
                                      self.screen.get_height()-40))

            # sensor icon & FOV
            sx,sy=self.mm_to_px(*self.sensor_mm)
            pygame.draw.circle(self.screen,C.GREEN,(sx,sy),6)
            if self.placing_sensor or (self.rotating_sensor and self.sensor_stage=="heading"):
                pygame.draw.rect(self.screen,C.GREEN,(sx-3,sy-3,6,6))
            if not self.placing_sensor:
                for ang in (-self.FOV_DEG/2,self.FOV_DEG/2):
                    th=math.radians(self.sensor_hd+ang)
                    ex=sx+math.sin(th)*5000/self.ppm
                    ey=sy-math.cos(th)*5000/self.ppm
                    pygame.draw.line(self.screen,C.DIM,(sx,sy),(ex,ey))
            if self.sensor_stage=="heading":
                th=math.radians(self.sensor_hd)
                ex=sx+math.sin(th)*60
                ey=sy-math.cos(th)*60
                pygame.draw.line(self.screen,C.GREEN,(sx,sy),(ex,ey),2)
                pygame.draw.circle(self.screen,C.GREEN,(ex,ey),4)

            # live targets & trails
            now=time.monotonic()
            if not self.map_mode:
                dash_y=self.off_y+self.svg_surf.get_height()+10
            for idx,(slot,x,y) in enumerate(self.latest):
                ser=self.tracker.slot2ser.get(slot)
                info=self.tracker.active.get(ser)
                if ser is None or info is None: continue
                ax,ay,av=self._avg_motion(ser,x,y,info['hist'][2])
                px,py=self.mm_to_px(*self.local_to_world(ax,ay))
                norm=min(1.0,av/self.MAX_V)
                gp=norm*(len(C.GRADIENT)-1); gi,fr=int(gp),gp-int(gp)
                col=tuple(int(C.GRADIENT[gi][c]*(1-fr)+C.GRADIENT[min(gi+1,3)][c]*fr)
                          for c in range(3))
                trail=self.trails.setdefault(ser,collections.deque())
                trail.append((px,py,now,col))
                while trail and now-trail[0][2]>self.trail_duration:
                    trail.popleft()
                if self.trail_on:
                    for tx,ty,tt,tc in trail:
                        alpha=int(255*(1-(now-tt)/self.trail_duration))
                        if alpha<=0 or (tx,ty)==(px,py): continue
                        dot=pygame.Surface((10,10),pygame.SRCALPHA)
                        pygame.draw.circle(dot,tc+(alpha,),(5,5),5)
                        self.screen.blit(dot,(tx-5,ty-5))
                ph=self.pulse_phase.get(ser,0.0)+dt_frame
                self.pulse_phase[ser]=ph%1.0
                pygame.draw.circle(self.screen,col,(px,py),int(10+ph*(20+60*norm)),1)
                pygame.draw.circle(self.screen,col,(px,py),5)
                self.screen.blit(C.FONT.render(ser,True,col),(px+8,py-8))
                if not self.map_mode:
                    rng=math.hypot(ax,ay)
                    txt=C.FONT.render(
                        f"{ser}: X={ax/1000:+.2f} Y={ay/1000:+.2f} "
                        f"D={rng/1000:.2f}m v={av/10:.1f}",True,col)
                    self.screen.blit(txt,(10,dash_y+idx*22))

            # footer current & recent
            if not self.map_mode:
                next_y=dash_y+len(self.latest)*22+5
                if self.latest:
                    cur=self.tracker.slot2ser[self.latest[0][0]]
                    inf=self.tracker.active.get(cur)
                    if inf:
                        dur=dt.datetime.now()-inf['first']
                        tl=C.SMALL_FONT.render(
                            f"{cur}  First {inf['first'].strftime('%H:%M:%S')}  "
                            f"Elapsed {str(dur).split('.')[0]}",
                            True,C.GREEN)
                        self.screen.blit(tl,(10,next_y)); next_y+=tl.get_height()+10
                self.screen.blit(C.SMALL_FONT.render("Recent Targets:",True,C.GREEN),
                                 (10,next_y))
                for i,tr in enumerate(self.tracker.recent):
                    sid=tr.get("serial","—")
                    txt=(f"{sid}: {tr['first'].strftime('%H:%M:%S')}–"
                         f"{tr['last'].strftime('%H:%M:%S')} "
                         f"({str(tr['dur']).split('.')[0]})")
                    self.screen.blit(C.SMALL_FONT.render(txt,True,C.GREEN),
                                     (10,next_y+20+i*18))

            # map exit button
            if self.map_mode:
                xs=C.MID_FONT.render("X",True,C.GREEN)
                self.exit_rect=xs.get_rect()
                self.exit_rect.topright=(self.screen.get_width()-10,10)
                pygame.draw.rect(self.screen,C.BLACK,self.exit_rect.inflate(8,4))
                self.screen.blit(xs,self.exit_rect)

            # data-loss banner
            if self.data_lost and self.flash:
                alert=C.BIG_FONT.render("DATA STREAM CONNECTION LOST",True,C.RED)
                ar=alert.get_rect(center=(self.screen.get_width()//2,
                                          self.screen.get_height()//2))
                self.screen.blit(alert,ar)

            # overlays
            if self.show_cfg: self._draw_cfg_popup()
            if self.sensor_stage=="intro": self._draw_sensor_intro()
            if self.night_mode:
                ov=pygame.Surface(self.screen.get_size(),pygame.SRCALPHA)
                ov.fill((255,0,0,120)); self.screen.blit(ov,(0,0))
            pygame.display.flip()

            # beep
            if self.latest and self.sound_on:
                n=min(1.0,self.fastest/self.MAX_V)
                freq=int(700+(1400-700)*n)
                if time.monotonic()-self._last_beep>=0.6-n*0.45:
                    beep(freq).play(); self._last_beep=time.monotonic()

        # graceful shutdown
        self._sync_cfg()
        if isinstance(self.reader,RadarMQTT):
            self.reader.cli.loop_stop()
        else:
            self.reader.stop()
        pygame.quit()
