# Kitchen Label System (Raspberry Pi + Zebra ZD411)

A lightweight kitchen labeling system designed for real-world use in a restaurant environment.  
Runs on a Raspberry Pi and prints labels directly to a Zebra ZD411 label printer using ZPL.

---

## Overview

This system allows kitchen staff to:
- Print food labels with stored and expiration dates
- Track items in a local SQLite database
- Reprint labels
- Mark items as completed ("done")

The system is accessible from any device on the local network via a web interface.

---

## Tech Stack

- Python 3.11
- Flask (web server)
- SQLite (local database)
- ZPL (Zebra Programming Language)
- Linux (Raspberry Pi OS Lite)
- systemd (service management)

---

## Hardware

- Raspberry Pi 5 (8GB RAM)
- Zebra ZD411 Label Printer (USB connection)

---

## How It Works

1. User opens web UI:
   http://192.168.50.4:5000

2. Inputs:
   - Item name
   - Shelf life (days)

3. System:
   - Calculates expiration date
   - Saves item to SQLite database
   - Generates ZPL label
   - Sends label to printer via `/dev/usb/lp0`

---

## Features

- Add + Print labels
- Reprint existing labels
- Mark items as done
- Days remaining tracking
- Mobile-friendly UI

---

## Printer Setup

- Printer connected via USB
- Uses Linux `usblp` driver
- Device path:
  `/dev/usb/lp0`

Ensure user is in `lp` group:
```bash
groups
