# Notifications

The terminal digest is always available: `jobhunt digest --human`. Push channels are optional
and go only to the user. Edit `~/.jobhunt/config.yaml`:

```yaml
notifications:
  ntfy_topic: jh-<long random string>   # ntfy.sh topics are public: make it unguessable
  discord_webhook: https://discord.com/api/webhooks/...   # a private channel
  smtp:                                  # email to yourself
    host: smtp.example.com
    port: 587
    username: you@example.com
    password_env: JOBHUNT_SMTP_PASSWORD  # the variable's NAME; never put the password here
    to: you@example.com
  digest: daily        # every_run | daily | off
  alerts: true         # push high-priority new postings right away
  alert_priority: 0.75
```

The user installs the ntfy app and subscribes to the topic, or creates the Discord webhook, or
sets the SMTP password variable themselves. Messages carry companies, titles, bands and links:
never answers or contact details. Then run `jobhunt notify test`.
