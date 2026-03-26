"""
Morning Scheduler
Runs the full pipeline automatically at a set time each weekday,
then launches the trade approval interface so you can review and
place trades over your morning coffee.
Run this once and leave it going in a terminal window:
    python3 run_morning.py
"""
import subprocess
import sys
import time
import os
from datetime import datetime

# ── Config ─────────────────────────────────────────────────────
RUN_HOUR   = 8   # 8:00 AM — pipeline runs before market open
RUN_MINUTE = 25   # adjust to your timezone (pipeline takes ~15 min)

# ── Helpers ────────────────────────────────────────────────────

def is_weekday():
    """Return True if today is Monday through Friday."""
    return datetime.now().weekday() < 5


def already_ran_today(log_file="data/scheduler_log.json"):
    """Return True if pipeline already ran successfully today."""
    try:
        import json
        with open(log_file, "r") as f:
            log = json.load(f)
        last_run = log.get("last_run", "")
        return last_run.startswith(datetime.now().strftime("%Y-%m-%d"))
    except FileNotFoundError:
        return False


def log_run(success, log_file="data/scheduler_log.json"):
    """Record the run result to avoid double-running."""
    import json
    os.makedirs("data", exist_ok=True)
    log = {
        "last_run": datetime.now().isoformat(),
        "success":  success
    }
    with open(log_file, "w") as f:
        json.dump(log, f, indent=2)


def run_pipeline():
    """Run the full pipeline and return True if it succeeded."""
    print(f"\n{'='*60}")
    print(f"🌅 MORNING PIPELINE STARTING")
    print(f"   {datetime.now().strftime('%A, %B %d %Y %H:%M:%S')}")
    print(f"{'='*60}\n")

    result = subprocess.run(
        [sys.executable, "pipeline/10_run_pipeline_tradier.py"]
    )

    return result.returncode == 0


def run_approval():
    """Launch the trade approval interface."""
    print(f"\n{'='*60}")
    print(f"💰 LAUNCHING TRADE APPROVAL")
    print(f"{'='*60}\n")

    subprocess.run(
        [sys.executable, "pipeline/11_place_trades.py"]
    )


def run_monitor():
    """Launch the position monitor in the background."""
    print(f"\n{'='*60}")
    print(f"🔍 LAUNCHING POSITION MONITOR")
    print(f"{'='*60}\n")

    subprocess.Popen(
        [sys.executable, "pipeline/12_position_monitor.py"],
        stdout=open("data/monitor_log.txt", "a"),
        stderr=subprocess.STDOUT
    )
    print("   Position monitor running in background")
    print("   Logs: data/monitor_log.txt")


# ── Main loop ──────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("⏰ MORNING SCHEDULER ACTIVE")
    print(f"   Pipeline runs at: {RUN_HOUR:02d}:{RUN_MINUTE:02d} "
          f"on weekdays")
    print(f"   Current time:     "
          f"{datetime.now().strftime('%H:%M:%S')}")
    print("=" * 60)
    print("\nPress Ctrl+C to stop\n")

    while True:
        try:
            now = datetime.now()

            # Check if it's time to run
            if (
                is_weekday()
                and now.hour == RUN_HOUR
                and now.minute == RUN_MINUTE
                and not already_ran_today()
            ):
                # Run pipeline
                success = run_pipeline()
                log_run(success)

                if success:
                    # Launch approval interface
                    run_approval()

                    # Start position monitor
                    run_monitor()
                else:
                    print("❌ Pipeline failed — check logs")

            else:
                # Show next run time every 30 minutes
                if now.minute % 30 == 0 and now.second < 10:
                    next_run = now.replace(
                        hour=RUN_HOUR,
                        minute=RUN_MINUTE,
                        second=0
                    )
                    if next_run < now:
                        # Already passed today — next run tomorrow
                        from datetime import timedelta
                        next_run = next_run + timedelta(days=1)
                        # Skip to next weekday
                        while next_run.weekday() >= 5:
                            next_run += timedelta(days=1)

                    print(f"[{now.strftime('%H:%M')}] "
                          f"Waiting... next run: "
                          f"{next_run.strftime('%A %H:%M')}")

            time.sleep(10)  # check every 10 seconds

        except KeyboardInterrupt:
            print("\n\n🛑 Scheduler stopped by user")
            break
        except Exception as e:
            print(f"❌ Scheduler error: {e}")
            time.sleep(60)


if __name__ == "__main__":
    main()
