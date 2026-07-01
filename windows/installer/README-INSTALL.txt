Voice-To-Text  -  Windows setup
===============================

Talk instead of type. Tap a key, speak, tap again -> your words appear wherever
your cursor is. Tap another key and say an instruction -> it writes or edits text
for you.

--------------------------------------------------------------------
HOW TO INSTALL  (about 2 minutes)
--------------------------------------------------------------------
1. Unzip this whole folder somewhere (Desktop is fine).

2. Double-click:   Install Voice-To-Text.bat

3. First time only, Windows may show a blue "Windows protected your PC" box.
   Click "More info", then "Run anyway". (The app is safe; it just isn't
   code-signed yet.)

4. The setup opens the Groq key page in your browser. Groq is the free service
   that does the transcription:
      - sign in (Google/email is fine)
      - click "Create API Key", copy it
      - paste it into the setup window and press Enter

5. Windows will pop up ONE permission prompt (UAC) so setup can turn on the
   microphone and add an antivirus exception. Click "Yes".

That's it. Look for the small amber microphone icon near the clock
(bottom-right). It also starts automatically each time you log in.

--------------------------------------------------------------------
HOW TO USE IT
--------------------------------------------------------------------
  * Dictate:   tap  F9 , speak, tap  F9  again  -> your words are typed.
  * Write/Edit: tap  Right Ctrl , say an instruction, tap again
                -> it drafts a reply, or edits whatever text you've selected.
  * Right-click the tray icon for Settings (change the keys), Restart, or Quit.

You can change the hotkeys any time in Settings.

--------------------------------------------------------------------
IF IT DOESN'T HEAR YOU  (error sound, no mic icon)
--------------------------------------------------------------------
  1. Settings > Privacy & security > Microphone: make sure Microphone access is
     ON, and "Let desktop apps access your microphone" is ON.
  2. If you run Bitdefender / Norton / McAfee, add this folder as an exception:
        %LocalAppData%\Programs\Voice-To-Text
     (Windows' own Defender is handled by the installer automatically.)
  3. Still stuck? Restart the PC once - it clears a wedged audio driver.

--------------------------------------------------------------------
NEEDS
--------------------------------------------------------------------
  * Windows 10 or 11
  * An internet connection (transcription runs in the cloud)
  * A free Groq API key (step 4 above)
