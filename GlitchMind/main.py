"""Запуск из исходников: python main.py   (в exe это же — точка входа PyInstaller)."""
import multiprocessing
import sys

from glitchmind.app import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
