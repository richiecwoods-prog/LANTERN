@echo off
setlocal
powershell -ExecutionPolicy Bypass -File "%~dp0LANTERN_App.ps1" %*
