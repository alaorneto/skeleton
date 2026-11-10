"""Delivery channels beyond the inbox: email (#54), digest (#55) and web push (#56).

Every channel writes a ``NotificationDelivery`` inside the transaction that decided it and
sends from a job afterwards: a rollback leaves neither the row nor the job, and a retry
never sends twice once the row is ``sent``.
"""
