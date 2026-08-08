@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0create-draft-from-package.ps1" %*
