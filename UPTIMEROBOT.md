# UptimeRobot Monitor Configuration
# 
# This file documents the UptimeRobot setup to keep your backend warm.
# Go to https://uptimerobot.com and create these monitors.
# Free tier: 50 monitors, 5-minute intervals.

# Monitor 1: Health Check (keeps server alive on serverless platforms)
# Type: Cron Job
# Cron: */5 * * * *
# Command: curl -s https://YOUR-BACKEND-URL/health
# 
# Monitor 2: API Endpoint Check
# Type: Cron Job
# Cron: */10 * * * *
# Command: curl -s https://YOUR-BACKEND-URL/v1/health

# For Render (always-on), you do NOT need UptimeRobot.
# Only use this if you deploy on a serverless platform.