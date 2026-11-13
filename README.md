# goldsrcengineonehtml
## Requirements
- Python 3.6 or newer (standard library only — no pip packages needed)
This repository contains the build pipeline and runtime glue to package a full GoldSrc game (Counter-Strike 1.6, Half-Life 1, Day of Defeat) into a single, self-contained HTML file that runs entirely offline in the browser via WebAssembly.

⚠️ Important: No Game Assets Included
This repository does not contain any copyrighted Valve assets. To use this tool, you must supply your own legally obtained valve.zip and cstrike.zip archives.

How It Works
The pipeline uses build_html.py to stream-read your game archives, Base64-encode them in 3MB chunks (to avoid RAM spikes), and inject them into template.html. The template uses Emscripten to mount the assets into a virtual filesystem and boot the Xash3D FWGS engine.

Features
Single-File Format: Entire engine + assets packed into one .html file.

CORS Bypass: Uses Base64 embedding to run from a file:// URL without a local server.

Custom Dev Console: Press . to open an in-game console to run commands like map de_dust2.

YaPB Bot Support: Includes the WASM side-module hooks for compiling and loading YaPB bots.

Usage
Clone this repository.

Place your legally obtained valve.zip and cstrike.zip in the root directory.

Install the dependencies: pip install -r requirements.txt (if applicable).

Run the build script: python3 build_html.py.

Open the generated cs16-offline.html in Chrome, Edge, or Opera GX.

License
This project is licensed under the MIT License. The Xash3D FWGS engine is licensed under the GPL. YaPB is licensed under its respective open-source license.
