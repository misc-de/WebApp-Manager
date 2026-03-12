# WebApp Manager

The WebApp Manager is a compact GTK/Libadwaita desktop tool for creating, importing, exporting, and managing Linux web app launchers with dedicated browser profiles.

## What it does
- Create and update web app launchers via a central user interface
- Manage dedicated Firefox and Chromium/Chrome profiles per web app
- Import existing launchers and .wapp exports
- Manage icons, user-agent preferences, and browser-specific options

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
<img width="300" alt="overview" src="https://github.com/user-attachments/assets/a950d663-34a0-4a29-a778-f5d1da9f7236" />
<br>
<img width="300" alt="detail" src="https://github.com/user-attachments/assets/ef80f234-4e66-4cf6-a04b-2c80838ece9f" />
