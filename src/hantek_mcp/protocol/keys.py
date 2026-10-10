"""Front-panel key codes for command 0x13 (key press emulation).

Key code == zero-based position of the entry in ``/keyprotocol.inf``. The
bundled DSO5102P copy (firmware 3.40.0) lists the same 49 keys in the same
order as the DSO5202B copy published on elinux.org, and upstream Test1.py
confirmed MENU-MEASURE (0x0C) and MENU-ACQUIRE (0x0D) on a DSO5102P.
"""

from __future__ import annotations

from enum import IntEnum


class Key(IntEnum):
    F0 = 0x00
    F1 = 0x01
    F2 = 0x02
    F3 = 0x03
    F4 = 0x04
    F5 = 0x05
    F6 = 0x06
    F7 = 0x07
    MULTI_LEFT = 0x08
    MULTI_RIGHT = 0x09
    MULTI_PRESS = 0x0A
    SAVE_RECALL = 0x0B
    MEASURE = 0x0C
    ACQUIRE = 0x0D
    UTILITY = 0x0E
    CURSOR = 0x0F
    DISPLAY = 0x10
    AUTOSET = 0x11
    SINGLE_SEQ = 0x12
    RUN_STOP = 0x13
    HELP = 0x14
    DEFAULT_SETUP = 0x15
    SAVE_TO_USB = 0x16
    MATH_MENU = 0x17
    CH1_MENU = 0x18
    CH1_POS_DOWN = 0x19
    CH1_POS_UP = 0x1A
    CH1_POS_ZERO = 0x1B
    CH1_VDIV_DOWN = 0x1C  # VBSUB: smaller V/div (more sensitive)
    CH1_VDIV_UP = 0x1D  # VBADD: larger V/div
    CH2_MENU = 0x1E
    CH2_POS_DOWN = 0x1F
    CH2_POS_UP = 0x20
    CH2_POS_ZERO = 0x21
    CH2_VDIV_DOWN = 0x22
    CH2_VDIV_UP = 0x23
    HORIZ_MENU = 0x24
    HORIZ_POS_SUB = 0x25
    HORIZ_POS_ADD = 0x26
    HORIZ_POS_ZERO = 0x27
    TIMEBASE_DOWN = 0x28  # TBSUB: faster timebase (smaller s/div)
    TIMEBASE_UP = 0x29  # TBADD: slower timebase
    TRIG_MENU = 0x2A
    TRIG_LEVEL_DOWN = 0x2B
    TRIG_LEVEL_UP = 0x2C
    TRIG_LEVEL_ZERO = 0x2D
    TRIG_LEVEL_50 = 0x2E
    FORCE_TRIG = 0x2F
    PROBE_CHECK = 0x30


#: keyprotocol.inf names in key-code order, used to cross-check against the instrument's file.
INF_NAMES: tuple[str, ...] = (
    "FN-0-KEY", "FN-1-KEY", "FN-2-KEY", "FN-3-KEY", "FN-4-KEY", "FN-5-KEY", "FN-6-KEY", "FN-7-KEY",
    "FN-MLEFT-KEY", "FN-MRIGHT-KEY", "FN-MZERO-KEY", "MENU-SR-KEY", "MENU-MEASURE-KEY",
    "MENU-ACQUIRE-KEY", "MENU-UTILITY-KEY", "MENU-CURSOR-KEY", "MENU-DISPLAY-KEY", "CT-AUTOSET-KEY",
    "CT-SINGLESEQ-KEY", "CT-RS-KEY", "CT-HELP-KEY", "CT-DS-KEY", "CT-STU-KEY", "VT-MATH-MENU-KEY",
    "VT-CH1-MENU-KEY", "VT-CH1-PSUB-KEY", "VT-CH1-PADD-KEY", "VT-CH1-PZERO-KEY", "VT-CH1-VBSUB-KEY",
    "VT-CH1-VBADD-KEY", "VT-CH2-MENU-KEY", "VT-CH2-PSUB-KEY", "VT-CH2-PADD-KEY", "VT-CH2-PZERO-KEY",
    "VT-CH2-VBSUB-KEY", "VT-CH2-VBADD-KEY", "HZ-MENU-KEY", "HZ-PSUB-KEY", "HZ-PADD-KEY",
    "HZ-PZERO-KEY", "HZ-TBSUB-KEY", "HZ-TBADD-KEY", "TG-MENU-KEY", "TG-PSUB-KEY", "TG-PADD-KEY",
    "TG-PZERO-KEY", "TG-PHALF-KEY", "TG-FORCE-KEY", "TG-PROBECHECK-KEY",
)  # fmt: skip

#: Keys whose effect is disruptive enough that only explicit, confirmed calls may press them.
DISRUPTIVE_KEYS = frozenset({Key.DEFAULT_SETUP, Key.SAVE_TO_USB, Key.SAVE_RECALL, Key.UTILITY})


def channel_keys(channel: int) -> dict[str, Key]:
    if channel == 1:
        return {
            "menu": Key.CH1_MENU,
            "pos_down": Key.CH1_POS_DOWN,
            "pos_up": Key.CH1_POS_UP,
            "pos_zero": Key.CH1_POS_ZERO,
            "vdiv_down": Key.CH1_VDIV_DOWN,
            "vdiv_up": Key.CH1_VDIV_UP,
        }
    if channel == 2:
        return {
            "menu": Key.CH2_MENU,
            "pos_down": Key.CH2_POS_DOWN,
            "pos_up": Key.CH2_POS_UP,
            "pos_zero": Key.CH2_POS_ZERO,
            "vdiv_down": Key.CH2_VDIV_DOWN,
            "vdiv_up": Key.CH2_VDIV_UP,
        }
    raise ValueError(f"invalid channel {channel}")
