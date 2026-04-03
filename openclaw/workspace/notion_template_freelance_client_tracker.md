# 💼 FREELANCE CLIENT TRACKER — Notion Template

## Product Info
- **Name:** Freelance Client & Project Hub
- **Price:** $7.99 (Gumroad) / 7.99€ (Ko-fi)
- **Category:** Business / Freelance
- **Target:** Freelancers, consultants, solopreneurs

---

## 🎯 SALES DESCRIPTION

### Title
**Freelance Client Hub — Track Projects, Invoices & Revenue in One Place**

### Tagline
*Stop juggling spreadsheets. One Notion template to run your entire freelance business.*

### Description
Built by a freelancer FOR freelancers. This template eliminates the chaos of managing clients, projects, tasks, and money across 10 different tools.

**What you get:**
✅ Client CRM with contact history
✅ Project tracker with status pipeline
✅ Task management per project
✅ Invoice tracker with payment status
✅ Revenue dashboard (monthly/yearly)
✅ Time tracking log
✅ Contract & document storage
✅ Proposal templates section
✅ Rate calculator (hourly ↔ project ↔ retainer)

**Perfect for:**
- Writers & content creators
- Designers & developers
- Consultants & coaches
- Virtual assistants
- Any service-based freelancer

**Why it works:**
- 📊 See your pipeline at a glance
- 💰 Know exactly what you're owed
- 🔗 Everything connected (client → projects → tasks → invoices)
- 📈 Track revenue growth over time

---

## 📋 TEMPLATE STRUCTURE

### DATABASE 1: Clients
| Property | Type | Purpose |
|----------|------|---------|
| Client Name | Title | Company or person |
| Contact Person | Text | Who you talk to |
| Email | Email | Primary contact |
| Phone | Phone | Optional |
| Industry | Select | Niche categorization |
| Source | Select | How they found you |
| Status | Select | Lead/Active/Paused/Past |
| Client Since | Date | First project date |
| Total Revenue | Rollup | Sum from projects |
| Projects | Relation | Link to projects DB |
| Rating | Select | ⭐⭐⭐⭐⭐ (private!) |
| Notes | Text | Context & history |

### DATABASE 2: Projects
| Property | Type | Purpose |
|----------|------|---------|
| Project Name | Title | Descriptive name |
| Client | Relation | Links to Clients DB |
| Type | Select | Retainer/One-time/Hourly |
| Status | Select | Lead/Proposal/Active/Review/Complete/Cancelled |
| Start Date | Date | Kickoff |
| Deadline | Date | Final delivery |
| Budget | Number | Agreed price |
| Paid Amount | Rollup | From invoices |
| Outstanding | Formula | Budget - Paid |
| Tasks | Relation | Link to tasks DB |
| Invoices | Relation | Link to invoices |
| Contract Link | URL | Google Drive/Dropbox |
| Notes | Text | Project specifics |

### DATABASE 3: Tasks
| Property | Type | Purpose |
|----------|------|---------|
| Task | Title | What to do |
| Project | Relation | Link to project |
| Status | Select | To-Do/In Progress/Review/Done |
| Priority | Select | 🔴High/🟡Medium/🟢Low |
| Due Date | Date | Deadline |
| Time Spent | Number | Hours logged |
| Notes | Text | Details |

### DATABASE 4: Invoices
| Property | Type | Purpose |
|----------|------|---------|
| Invoice # | Title | INV-001 format |
| Project | Relation | Links to project |
| Client | Rollup | Auto from project |
| Amount | Number | Invoice total |
| Issue Date | Date | When sent |
| Due Date | Date | Payment deadline |
| Status | Select | Draft/Sent/Paid/Overdue |
| Paid Date | Date | When received |
| Payment Method | Select | Bank/PayPal/Wise/Other |
| Notes | Text | Details |

### DATABASE 5: Time Log
| Property | Type | Purpose |
|----------|------|---------|
| Entry | Title | What you worked on |
| Project | Relation | Link to project |
| Date | Date | When |
| Hours | Number | Time spent |
| Billable | Checkbox | Count toward client |
| Rate | Number | Hourly rate used |
| Value | Formula | Hours × Rate |

---

## 📊 VIEWS

### Client Views
1. **Active Clients** — Only status = Active
2. **Client Pipeline** — Kanban by status
3. **Top Clients** — Sorted by total revenue
4. **Acquisition Source** — Grouped by how they found you

### Project Views
1. **Project Pipeline** — Kanban board (Lead→Complete)
2. **This Month** — Calendar view
3. **Active Projects** — List, sorted by deadline
4. **Revenue by Project** — Table with budget column

### Financial Views
1. **Invoices Due** — Filtered to Sent + Overdue
2. **Monthly Revenue** — Grouped by month
3. **Unpaid Total** — Sum of outstanding
4. **Payment History** — All paid invoices

### Task Views
1. **My Tasks Today** — Due today or overdue
2. **By Project** — Grouped by project
3. **Priority Matrix** — Sorted by priority then date

---

## 📈 DASHBOARD WIDGETS

### Revenue Tracker
```
| Metric | Formula |
|--------|---------|
| This Month | Sum invoices paid this month |
| Last Month | Sum invoices paid last month |
| YTD | Sum all paid invoices this year |
| Outstanding | Sum of unpaid invoices |
| Pipeline Value | Sum of project budgets in Lead/Proposal |
```

### Quick Stats
- Active clients count
- Active projects count
- Tasks due this week
- Invoices overdue

### Monthly Goal Tracker
- Set monthly revenue target
- Progress bar toward goal
- Days remaining in month

---

## 🧮 RATE CALCULATOR (BONUS)

### Hourly to Project Converter
```
Estimated hours × Hourly rate × Buffer (1.2) = Project quote
```

### Annual Income Goal Calculator
```
Annual goal ÷ Working months ÷ Billable hours = Required hourly rate
```

### Retainer Calculator
```
Monthly hours × Rate × Discount (0.9) = Monthly retainer
```

---

## 📄 INCLUDED TEMPLATES

### Proposal Template
- Project scope section
- Timeline
- Investment (pricing)
- Terms & conditions
- Signature block

### Welcome Email Template
- Onboarding info
- Communication preferences
- What you need from client
- Next steps

### Invoice Template
- Professional layout
- Line items
- Payment details
- Due date prominent

### Offboarding Email
- Project wrap-up
- Final deliverables
- Testimonial request
- Referral ask

---

## 🎨 CUSTOMIZATION

### Color Themes
1. **Professional Blue** — Clean, corporate-friendly
2. **Creative Warm** — Orange/coral tones
3. **Minimal Mono** — Black/white/gray

### Industry Presets
- Writer/Content (retainer-focused)
- Designer (project-focused)
- Developer (hourly + project mix)
- Consultant (retainer + calls)

---

## 📦 DELIVERABLES

1. **Notion Template** (full duplicate link)
2. **Setup Guide** (PDF, 4 pages)
3. **Walkthrough Video** (Loom, 8 min)
4. **Email Templates** (bonus .txt file)

---

## 🏷️ TAGS

notion template, freelance, client management, CRM, project tracker, invoice tracker, freelancer tools, solopreneur, consultant, time tracking, revenue dashboard, business organization

---

## 💰 PRICING

| Platform | Price | Rationale |
|----------|-------|-----------|
| Gumroad | $7.99 | Higher value = higher price |
| Ko-fi | 7.99€ | Match pricing |
| Notionery | $9.99 | Premium positioning |

**Bundle:** Add "Freelance Proposal Pack" (3 templates) for $14.99 total

---

## 📈 MARKETING ANGLES

### Twitter/X
"I replaced 5 tools with 1 Notion template. Here's my freelance client system (and why spreadsheets were killing my business)..."

### LinkedIn
Perfect for reaching B2B freelancers. Post about client management pain points.

### Reddit
r/freelance, r/Notion, r/Entrepreneur — Share genuine value about systems

### Lead Magnet Option
Free "Lite" version: Just Client + Project DB, no invoicing/time tracking
