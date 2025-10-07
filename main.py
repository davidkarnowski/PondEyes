"""
Entry-point.  Keeps top-level script tiny.
"""
import pygame
from radar import config, gui

def main():
    pygame.init()
    cfg = config.load()
    app = gui.RadarGUI(cfg)
    app.run()
    config.save(cfg)

if __name__ == "__main__":
    main()

