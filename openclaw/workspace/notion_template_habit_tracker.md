# 📊 HABIT TRACKER PRO — Notion Template

## Product Info
- **Name:** Habit Tracker Pro
- **Price:** $4.99 (Gumroad) / 4.99€ (Ko-fi)
- **Category:** Productivity / Self-improvement
- **Target:** People wanting to build consistent habits

---

## 🎯 SALES DESCRIPTION

### Title
**Habit Tracker Pro — Build Lasting Habits in 66 Days**

### Tagline
*The science-backed Notion system to transform your daily routines into unstoppable habits*

### Description
Stop failing at New Year resolutions. This template uses the proven 66-day habit formation method, combined with visual progress tracking and streak mechanics to make habit building actually enjoyable.

**What you get:**
✅ Master dashboard with all habits at a glance
✅ Daily check-in system (takes 30 seconds)
✅ Visual progress bars & streak counters
✅ Weekly review template
✅ Monthly analytics view
✅ "Habit Stack" builder (link habits together)
✅ Emergency "I Broke My Streak" recovery protocol
✅ 20 pre-loaded habit ideas with triggers

**Perfect for:**
- Building morning/evening routines
- Fitness & health goals
- Learning new skills
- Breaking bad habits
- Digital detox tracking

**Why this template works:**
- 🧠 Based on James Clear's "Atomic Habits" principles
- 📈 Visual dopamine hits with progress bars
- 🔥 Streak motivation (don't break the chain!)
- 📊 Data to see what actually sticks

---

## 📋 TEMPLATE STRUCTURE

### DATABASE: Habits Master
| Property | Type | Purpose |
|----------|------|---------|
| Habit Name | Title | Name of the habit |
| Category | Select | Health/Work/Learning/Social/Creative |
| Frequency | Select | Daily/Weekdays/3x Week/Weekly |
| Trigger | Text | What cues this habit |
| Reward | Text | Reward after completion |
| Current Streak | Formula | Auto-calculated |
| Best Streak | Number | Personal record |
| Start Date | Date | When you began |
| Status | Select | Active/Paused/Completed/Dropped |
| 66-Day Progress | Formula | % toward habit formation |

### DATABASE: Daily Check-ins
| Property | Type | Purpose |
|----------|------|---------|
| Date | Date | Check-in date |
| Habits Completed | Relation | Link to completed habits |
| Mood | Select | 😴😐😊😁🔥 |
| Energy Level | Select | 1-5 |
| Notes | Text | Optional reflection |
| Score | Formula | % habits completed |

### VIEWS

**1. Today's Habits**
- Filtered to show only today's active habits
- Simple checkbox interface
- Shows current streak for motivation

**2. Weekly Overview**
- Calendar view of the week
- Color-coded by completion rate
- Quick patterns visible

**3. Streak Leaderboard**
- Sorted by current streak
- Shows habits you're winning at
- Gamification element

**4. 66-Day Progress**
- Gallery view of all habits
- Progress bars toward 66 days
- Celebration when habit is "locked in"

**5. Analytics Dashboard**
- Overall completion rate
- Best performing day of week
- Mood vs habits correlation

---

## 📝 PRE-LOADED HABIT IDEAS

### 🏃 Health & Fitness
1. Drink 2L water
2. 10-minute walk
3. No phone first hour
4. Sleep by 11pm
5. 5-minute stretch

### 🧠 Learning & Growth
6. Read 10 pages
7. Learn 5 new words
8. Listen to podcast
9. Write in journal
10. Practice skill 15min

### 💼 Work & Productivity
11. Plan tomorrow tonight
12. Deep work block
13. Inbox zero
14. One task before email
15. Weekly review

### 🧘 Mindfulness
16. 5-min meditation
17. Gratitude log
18. Digital sunset 9pm
19. No social media morning
20. Breathing exercise

---

## 🆘 STREAK RECOVERY PROTOCOL

When you break a streak (it happens!):

### The 2-Day Rule
*Never miss twice in a row*

**Recovery Steps:**
1. **Don't catastrophize** — One miss ≠ failure
2. **Log why you missed** — Identify the blocker
3. **Reduce the habit** — Make it tiny (5 pushups → 1 pushup)
4. **Recommit immediately** — Do a micro-version NOW
5. **Update your trigger** — Maybe the cue needs changing

### Auto-generated "Recovery Mode"
- Template includes a view that activates when streak breaks
- Shows smaller version of the habit
- Celebrates getting back on track

---

## 📊 FORMULAS INCLUDED

### Current Streak Calculator
```
prop("Last 7 Days Completed") filtered and counted
```

### 66-Day Progress
```
min(100, round(dateBetween(now(), prop("Start Date"), "days") / 66 * 100))
```

### Weekly Score
```
sum(relation("Daily Check-ins").filter(thisWeek)) / 7 * 100
```

---

## 🎨 AESTHETIC OPTIONS

Template includes 3 color themes:
1. **Minimal Dark** — Clean, professional
2. **Colorful Light** — Energetic, fun
3. **Nature Tones** — Calm, earthy

---

## 📦 DELIVERABLE FILES

1. **Notion Template Link** (duplicable)
2. **Quick Start Guide** (PDF, 2 pages)
3. **Video Walkthrough** (Loom, 5 min) — *optional, adds value*

---

## 🏷️ TAGS FOR DISCOVERY

notion template, habit tracker, productivity, atomic habits, streak tracker, daily routine, self improvement, goal setting, personal development, habit building, 66 days, routine tracker

---

## 💰 PRICING STRATEGY

| Platform | Price | Why |
|----------|-------|-----|
| Gumroad | $4.99 | Mid-range, impulse buy zone |
| Ko-fi | 4.99€ | Same positioning |
| Notionery | $6.99 | Higher prices expected there |

**Bundle potential:** Combine with Goal Tracker + Journal template for $12.99

---

## 📈 MARKETING ANGLES

### Twitter/X Thread
"I tracked my habits for 66 days. Here's the science of why it works (and the free/paid template I used)..."

### Reddit (r/Notion, r/productivity)
Value post about habit stacking, mention template at end

### Lead Magnet Option
Offer "Lite" version (3 habits only) free to build email list
