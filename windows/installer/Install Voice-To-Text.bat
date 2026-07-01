@echo off
title Voice-To-Text Setup
REM Double-click this file to install Voice-To-Text.
REM It runs the setup script (which asks for one admin approval partway through).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
