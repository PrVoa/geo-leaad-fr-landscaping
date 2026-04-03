# Notion Template: Side Hustle Dashboard

## Product Info
**Name:** Side Hustle Command Center
**Price:** $9.99 (Full) / FREE (Lite)
**Target:** People with side hustles or starting one
**USP:** Track income, time, and progress — know your real $/hour

---

## 🎨 Template Structure

### PAGE 1: 📊 Dashboard (Home)

**Header Section**
```
🚀 SIDE HUSTLE COMMAND CENTER
"Your side hustle isn't a hobby. Treat it like a business."
```

**Metrics Row (Synced with databases)**
| This Month | | Year to Date |
|------------|---|--------------|
| 💰 Income: $XXX | | 💰 Total: $X,XXX |
| ⏱️ Hours: XX | | ⏱️ Hours: XXX |
| 💵 $/Hour: $XX | | 💵 Avg $/Hour: $XX |
| 📈 vs Last Month: +X% | | 🎯 Goal Progress: XX% |

**Progress Bar**
```
Goal: $500/month
[████████░░░░░░░░░░░░] 42% — $210 earned
```

**Quick Actions (Buttons)**
- ➕ Log Income
- ⏱️ Log Time
- 💡 Add Idea
- 📋 Weekly Review

**Recent Activity (Linked view)**
- Last 5 income entries
- Last 5 time logs

---

### PAGE 2: 💰 Income Tracker

**Database Properties:**
- Date (date)
- Amount (number, currency)
- Source (select: Platform A, Client B, Product C, etc.)
- Category (select: Service, Product, Affiliate, Other)
- Side Hustle (relation to Ideas database)
- Notes (text)
- Recurring (checkbox)

**Views:**
1. **📋 All Income** (Table, sorted by date desc)
2. **📅 Calendar** (Calendar view by date)
3. **📊 By Source** (Table grouped by Source)
4. **📈 Monthly Summary** (Table grouped by month)

**Formulas to include:**
- Total this month
- Average transaction
- Biggest single income

---

### PAGE 3: ⏱️ Time Tracker

**Database Properties:**
- Date (date)
- Hours (number)
- Activity (select: Client work, Marketing, Admin, Learning, Creating)
- Side Hustle (relation)
- Notes (text)
- Billable (checkbox)

**Views:**
1. **📋 All Time** (Table)
2. **📅 This Week** (Table filtered)
3. **📊 By Activity** (Board grouped by Activity)
4. **📈 Weekly Totals** (Table grouped by week)

**Key Metric Callout:**
```
⚠️ Your effective hourly rate this month: $XX
(Total income ÷ Total hours)

Industry comparison:
- < $15/hr: Consider pivoting
- $15-30/hr: Growing nicely
- $30-50/hr: Solid side hustle
- $50+/hr: Scale this!
```

---

### PAGE 4: 💡 Ideas Backlog

**Database Properties:**
- Idea Name (title)
- Category (select: New Side Hustle, Product, Service, Marketing, Improvement)
- Effort (select: Low/Medium/High)
- Potential (select: $/$$/$$$)
- Status (select: 💭 Idea, 🔍 Researching, 🧪 Testing, ✅ Active, ❌ Abandoned)
- Why Abandoned (text, show when status = Abandoned)
- Notes (text)
- Links (URL)

**Views:**
1. **🎯 Kanban** (Board by Status)
2. **📋 All Ideas** (Table)
3. **⭐ High Potential** (Table filtered by Potential = $$$)
4. **⚡ Quick Wins** (Table filtered by Effort = Low)

---

### PAGE 5: 🎯 Goals & Milestones

**Monthly Goals Section**
```
## April 2026 Goals

Income Target: $500
□ Milestone: First $100 week
□ Milestone: 5 new customers
□ Milestone: Launch new product

Time Budget: 20 hours
□ Max 5 hours on admin
□ Min 10 hours on revenue-generating
```

**Milestone Tracker (Database)**
| Milestone | Target | Status | Date Achieved |
|-----------|--------|--------|---------------|
| First $1 | $1 | ✅ | Mar 15 |
| First $10 | $10 | ✅ | Mar 22 |
| First $100 | $100 | 🔄 | - |
| First $500 | $500 | ⏳ | - |
| First $1,000 | $1,000 | ⏳ | - |
| $500/month | $500/mo | ⏳ | - |
| $1,000/month | $1K/mo | ⏳ | - |
| Replace day job | $X,XXX/mo | ⏳ | - |

---

### PAGE 6: 📚 Resources

**Tools I Use**
| Tool | Purpose | Cost | Link |
|------|---------|------|------|
| Canva | Graphics | Free | [link] |
| Notion | Organization | Free | [link] |
| ... | ... | ... | ... |

**Accounts & Logins Reference**
| Platform | Username | Email | Notes |
|----------|----------|-------|-------|
| (Don't store passwords here!) |

**Learning Resources**
- [ ] Course: ...
- [ ] Book: ...
- [ ] YouTube: ...

**Templates & Files**
- Link to invoice template
- Link to contract template
- Link to portfolio

---

### PAGE 7: 📝 Weekly Review

**Template for Weekly Reviews:**
```
# Week of [DATE]

## 📊 Numbers
- Income: $
- Hours: 
- $/Hour: $
- vs. Last Week: +/- %

## ✅ What Worked
- 

## ❌ What Didn't
- 

## 💡 Lessons Learned
- 

## 🎯 Next Week Focus
1. 
2. 
3. 

## 🚧 Blockers
- 
```

---

## 📦 LITE VERSION (Free Lead Magnet)

Includes only:
- Dashboard (simplified)
- Income Tracker
- Basic Goals page

**CTA on Lite version:**
```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🚀 WANT THE FULL VERSION?

Get the complete Side Hustle Command Center:
✅ Time Tracker with $/hour calculations
✅ Ideas Backlog with Kanban
✅ Milestone Tracker
✅ Weekly Review templates
✅ Resources hub
✅ Lifetime updates

→ [Get Full Version — $9.99]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

---

## 🎯 Marketing Angles

**Pain points addressed:**
- "I don't know if my side hustle is actually profitable"
- "I have no idea how many hours I'm putting in"
- "I keep having ideas but never act on them"
- "I need to treat this more seriously"

**Headlines:**
- "Your side hustle deserves better than a spreadsheet"
- "Know your REAL hourly rate (it might surprise you)"
- "From hobby to business in one dashboard"
- "The system that helped me hit $1K/month on the side"

---

## 📋 Creation Checklist

- [ ] Create Notion template with all pages
- [ ] Set up databases with correct properties
- [ ] Create views for each database
- [ ] Add formulas for calculations
- [ ] Design dashboard layout
- [ ] Add example data
- [ ] Create Lite version (duplicate, remove pages)
- [ ] Test template sharing/duplication
- [ ] Write Gumroad listing
- [ ] Create cover image

---

## 💰 Pricing Strategy

**Lite:** FREE
- Lead magnet
- Captures email
- Upsell to full version

**Full:** $9.99
- One-time purchase
- Lifetime updates
- Could bundle with other templates later

**Bundle idea:** "Side Hustle Starter Kit" — $19.99
- Side Hustle Dashboard
- Freelance Client Hub
- Content Creator Planner
- All prompt packs
