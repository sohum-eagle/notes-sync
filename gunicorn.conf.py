def post_fork(server, worker):
    from apscheduler.schedulers.background import BackgroundScheduler
    from main import _run_granola_sync
    scheduler = BackgroundScheduler()
    scheduler.add_job(_run_granola_sync, "interval", minutes=15)
    scheduler.start()
