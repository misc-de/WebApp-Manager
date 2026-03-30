# WebApp Manager

The WebApp Manager is a compact GTK/Libadwaita desktop and mobile tool for creating, importing, exporting, and managing Linux web app launchers with dedicated browser profiles.

⚠️ **AI-assisted project**  
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

## Pictures

**Desktop**  
<img width="800" height="1577" alt="1" src="https://github.com/user-attachments/assets/49c3d098-7ccc-46e0-ad7c-75dac21cc3fb" />

**Mobile**  
<img width="400" height="1663" alt="2" src="https://github.com/user-attachments/assets/53cb8d1b-3f86-4d43-97cd-7b9d1445cb90" />
<img width="400" height="1673" alt="3" src="https://github.com/user-attachments/assets/f83d1042-20e8-4ab2-8ff4-8fba5a6b0177" />
