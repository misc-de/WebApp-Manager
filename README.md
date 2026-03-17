# WebApp Manager

The WebApp Manager is a compact GTK/Libadwaita desktop and mobile tool for creating, importing, exporting, and managing Linux web app launchers with dedicated browser profiles.

⚠️ **Work in progress**  
This project is under active development. Features may change and instability is possible.

## What it does
- Create and update web app launchers via a central user interface
- Manage dedicated Firefox and Chromium/Chrome profiles per web app
- Import existing launchers and .wapp exports
- Manage icons, user-agent preferences, modes and browser-specific options

## Installation
```
git clone https://github.com/misc-de/WebApp-Manager.git
```


## Startup
After the first launch, a web app launcher is automatically created on the system.
```
python3 WebApp-Manager/main.py
```


## Import
Web apps can be imported via .wapp files.

Existing launchers can be extended with the value
```
ManagedBy=WebApp  
```

in the existing .desktop file so that they are imported when the WebApp Manager is started.

## Third-party browser plugins

This project can work with third-party Firefox extensions. Those extensions are **not authored by this project** and remain under their **upstream licenses and distribution terms**.

Current plugin references:

- **uBlock Origin**  
  AMO: https://addons.mozilla.org/firefox/addon/ublock-origin/  
  Project: https://github.com/gorhill/uBlock

- **Simple Swipe Navigator**  
  AMO: https://addons.mozilla.org/en-US/android/addon/simple-swipe-navigator/  
  Source: https://github.com/usemoslinux/swipe-navigator

## Pictures

**Desktop**  
<img width="1200" alt="1" src="https://github.com/user-attachments/assets/cbbda8ed-f8ee-4da3-af70-ec60a9840965" /><br />

**Mobile**  
<img width="300" alt="2" src="https://github.com/user-attachments/assets/8f9cf499-f55e-4c98-936a-4566c8d62667" />
<img width="300" alt="3" src="https://github.com/user-attachments/assets/849b8783-0d65-4cf8-8d8f-f9d5848fd514" />
<img width="300" alt="4" src="https://github.com/user-attachments/assets/5bab28e0-6f1c-4777-af73-e4de47affa21" />
