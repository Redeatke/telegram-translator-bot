#!/usr/bin/env python3
"""
Windows Task Scheduler Setup — Schedules refresh_cookies.py to run automatically
every 5 days so your bot always has fresh YouTube cookies on Render.

Usage:
    python schedule_refresh.py

Run as Administrator if you want the task to run even when not logged in.
"""

import sys
import os
import subprocess
from pathlib import Path

# Fix Windows console encoding for emoji support
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def main():
    print()
    print("=" * 55)
    print("  🗓️   YouTube Cookie Auto-Scheduler Setup")
    print("=" * 55)
    print()

    # Paths
    python_exe   = sys.executable
    script_dir   = Path(__file__).parent.resolve()
    script_path  = script_dir / "refresh_cookies.py"
    task_name    = "YouTubeCookieRefresh_TelegramBot"

    if not script_path.exists():
        print("❌  refresh_cookies.py not found in this directory.")
        sys.exit(1)

    # Build the command that Task Scheduler will run
    # Runs every 5 days at 9:00 AM
    task_cmd = f'"{python_exe}" "{script_path}"'

    print(f"  Python     : {python_exe}")
    print(f"  Script     : {script_path}")
    print(f"  Task name  : {task_name}")
    print(f"  Schedule   : Every 5 days at 9:00 AM")
    print()

    # schtasks XML approach for more control
    xml_content = f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Refreshes YouTube cookies for the Telegram bot and pushes them to Render.</Description>
  </RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>2024-01-01T09:00:00</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByDay>
        <DaysInterval>5</DaysInterval>
      </ScheduleByDay>
    </CalendarTrigger>
  </Triggers>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <ExecutionTimeLimit>PT10M</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{python_exe}</Command>
      <Arguments>"{script_path}"</Arguments>
      <WorkingDirectory>{script_dir}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>"""

    # Write temp XML file
    xml_path = script_dir / "_task_temp.xml"
    xml_path.write_text(xml_content, encoding="utf-16")

    try:
        # Delete existing task if present (ignore error if not found)
        subprocess.run(
            ["schtasks", "/Delete", "/TN", task_name, "/F"],
            capture_output=True
        )

        # Create the task from XML
        result = subprocess.run(
            ["schtasks", "/Create", "/TN", task_name, "/XML", str(xml_path)],
            capture_output=True,
            text=True,
        )

        if result.returncode == 0:
            print("  ✅  Task created successfully!")
            print()
            print("  The cookie refresh will run automatically every 5 days.")
            print("  Your bot on Render will always have fresh YouTube cookies.")
            print()
            print("  To run it manually right now:")
            print(f"    schtasks /Run /TN {task_name}")
            print()
            print("  To view the task in Task Scheduler:")
            print("    taskschd.msc")
        else:
            print(f"  ❌  Failed to create task: {result.stderr}")
            print()
            print("  Try running this script as Administrator.")
            print("  Or manually create a scheduled task pointing to:")
            print(f"    {python_exe} {script_path}")
    finally:
        if xml_path.exists():
            xml_path.unlink()

    print()


if __name__ == "__main__":
    main()
