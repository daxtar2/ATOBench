#!/usr/bin/env python3
from __future__ import annotations

import time

if "--version" in __import__("sys").argv:
    print("2.1.156-timeout-fake")
else:
    time.sleep(5)
