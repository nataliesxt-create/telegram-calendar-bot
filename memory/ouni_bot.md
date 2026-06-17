---
name: ouni-telegram-bot
description: Ouni — Natalie's Telegram calendar agent bot, built on Google Calendar + OpenAI
metadata:
  type: project
---

# Ouni — Telegram Calendar Agent

**What it is:** A personal Telegram bot that manages Natalie's Google Calendar via natural language text and voice notes.

**Why:** Natalie: Built to replace manual calendar entry — she texts or voice-notes Ouni from Telegram and it creates, edits, deletes, and views events across all her Google calendars.

**How to apply:** When Natalie references "Ouni", "the Telegram bot", or "the calendar bot", this is the project.

---

## Stack
- Python 3.11+ on Railway (perceptive-encouragement project, telegram-calendar-bot service)
- python-telegram-bot v21
- Google Calendar API (OAuth 2.0, single user)
- OpenAI GPT-4o-mini (intent parsing) + Whisper (voice transcription)
- GitHub: nataliesxt-create/telegram-calendar-bot

## Calendar Routing Rules
1. **Self Care + Activities** — lash, nails, facial, massage, salon, spa, brow, wax, hair, dentist, doctor, physio, therapy
2. **Appointments** — client bookings only (Natalie is the provider): "appointment with [name]", "book [name] in"
3. **West Family** — anything with Ellie, Chris, or family
4. **branding dept** — branding-related
5. **Roadshows** — roadshow events
6. **Travels + Work Trips** — travel, flights, hotels
7. **Social Media & Content** — filming, content, photoshoot, social media
8. **Birthdays** — birthdays
9. **Routines** — routines, habits
10. **Commute and Activities** — commute, school run
11. **Growth-centric** — courses, learning, workshops
12. **Bloom Into Legacy** — Bloom Into Legacy brand
13. **Champagne & Chaos** — Champagne & Chaos brand
14. **Work** — construction: architects, HDB, MOE, contractors, site visits
15. **Team Meetings + Events** — only when user says "team"
16. **Agency Events + Meetings** — company/agency events (Great Eastern, Craigasabel, congresses)
17. **ASK** — anything unclear

## Appointments Calendar Special Rules
- Duration: always 90 minutes
- Placeholder blocks: red "Appointment:" events = available slots
- Booking flow: user specifies time → bot proposes → user confirms yes/no → red placeholder deleted, green confirmed event created
- Color: Basil (10) = confirmed green

## Clash Detection
- Excludes: pfgroadshow@gmail.com, SC Direct Group, TEAM MEETING calendars

## Token Persistence
- GOOGLE_TOKEN_JSON stored as Railway env var (set manually via CLI after each auth)
- Railway API token (RAILWAY_API_TOKEN) attempts auto-update via GraphQL on each auth
