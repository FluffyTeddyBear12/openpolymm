Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = "d:\neststock\scripts\polymarket_bot"
WshShell.Run """d:\neststock\scripts\polymarket_bot\venv\Scripts\python.exe"" start_bot.py", 0, False
