@echo off

rem Set environment variables
set "LEAPSDK_INSTALL_LOCATION=C:\Program Files (x86)\Steam\steamapps\common\Ultraleap Gemini\LeapSDK"
set "RPI_HOST=192.168.1.102"
set "PROJECT_AXES=x,z"

rem Activate virtual environment using full path
call "C:\Users\Creative Machine 02\leapenv\Scripts\activate.bat"

rem Change directory to project folder
cd /d "C:\Users\Creative Machine 02\Desktop\leapc-python-bindings-main"

rem Run the script
python leap_sender.py

pause