# Morning Oil Brief — Professional Deployment Guide
## Stack: GitHub → Vercel → Supabase

---

## Overview

| Layer | Tool | Purpose |
|---|---|---|
| Auth & user database | Supabase | Stores users, hashes passwords, sends emails |
| Code version control | GitHub | Single source of truth for all your files |
| Hosting & CDN | Vercel | Serves your site globally, auto-deploys on every push |
| Domain | morningoilbrief.com | Your custom domain, connected through Vercel |

The professional workflow: **edit files locally → push to GitHub → Vercel auto-deploys in ~30 seconds.**
You never manually upload files again.

---

## Phase 1 — GitHub Setup

### 1.1 Create a GitHub account
Go to https://github.com and sign up if you don't have one.

### 1.2 Create a new private repository
1. Click **New** (top left, green button)
2. Name it: `morning-oil-brief`
3. Set visibility to **Private** — your code and data files stay out of public view
4. Click **Create repository**

### 1.3 Install GitHub Desktop (easiest for non-developers)
Download from https://desktop.github.com — this gives you a visual interface
to sync your local files to GitHub without using the command line.

1. Open GitHub Desktop → **File → Add Local Repository**
2. Point it at your EIA Dashboard folder
3. It will detect all your files as new changes
4. Write a commit message like "Initial setup" → click **Commit to main**
5. Click **Publish repository** → choose your `morning-oil-brief` repo → push

Your files are now on GitHub.

---

## Phase 2 — Supabase Setup

### 2.1 Create a Supabase account & project
1. Go to https://supabase.com → sign up
2. Click **New Project**
3. Name: `morning-oil-brief`
4. Set a strong **database password** — save this in a password manager
5. Region: **US East (N. Virginia)** — close to most US users
6. Click **Create new project** — takes ~2 minutes

### 2.2 Get your API keys
1. Go to **Project Settings → API** (gear icon, bottom left sidebar)
2. Copy and save these two values somewhere safe:
   - **Project URL** — e.g. `https://abcxyz.supabase.co`
   - **anon / public key** — long string starting with `eyJ...`

> ⚠️ Never paste these keys directly into your HTML files. See Phase 3 for the
> correct way to handle them using Vercel environment variables.

### 2.3 Configure Auth settings
1. Go to **Authentication → URL Configuration**
2. Set **Site URL**: `https://www.morningoilbrief.com`
3. Add **Redirect URLs**:
   - `https://www.morningoilbrief.com/login.html`
   - `https://www.morningoilbrief.com/index.html`
   - `http://localhost:3000/login.html` ← for local testing
4. Save

### 2.4 Customize email templates (optional but recommended)
1. Go to **Authentication → Email Templates**
2. Edit **Confirm signup** — add your branding, name, description
3. Edit **Reset password** — same
4. Users will see these when they sign up or reset their password

---

## Phase 3 — Vercel Setup

### 3.1 Create a Vercel account
Go to https://vercel.com → **Sign up with GitHub** (use the same GitHub account).
This links Vercel directly to your repos.

### 3.2 Import your project
1. In Vercel dashboard click **Add New → Project**
2. Find and select your `morning-oil-brief` GitHub repo
3. Click **Import**
4. Leave all build settings as default (it detects static HTML automatically)
5. **Before clicking Deploy** — set up environment variables (next step)

### 3.3 Add environment variables
This is the professional way to handle API keys — they live in Vercel's secure
vault, not in your code files.

In the Vercel import screen, scroll to **Environment Variables** and add:

| Name | Value |
|---|---|
| `NEXT_PUBLIC_SUPABASE_URL` | your Supabase Project URL |
| `NEXT_PUBLIC_SUPABASE_ANON_KEY` | your Supabase anon key |

> These variables are injected at build time and never exposed in your GitHub repo.
> Anyone who clones your repo cannot access your Supabase project.

Click **Deploy**. Vercel builds and deploys your site in ~30 seconds.

### 3.4 Note on environment variables in static HTML
Since your site is plain HTML (not a Node/React app), Vercel environment variables
aren't automatically injected into HTML files the same way they are in JS frameworks.

For plain HTML, you have two options:

**Option A — Keep keys in the HTML files (simpler, acceptable for public anon keys)**
The Supabase `anon` key is designed to be public — it only allows what your
Row Level Security (RLS) policies permit. Paste the keys directly in login.html
and the auth guard scripts. This is common practice for Supabase + static sites.

**Option B — Add a lightweight build step (more professional)**
Use a simple Node script or Vercel Edge Function to inject keys at build time.
This is worth doing when you add paid tiers or sensitive logic later.

**Recommendation: Start with Option A.** The Supabase anon key is safe to expose.
Switch to Option B when you add paid subscription logic.

---

## Phase 4 — Connect Your Domain

### 4.1 Add domain in Vercel
1. In your Vercel project → **Settings → Domains**
2. Click **Add Domain** → type `morningoilbrief.com`
3. Also add `www.morningoilbrief.com`
4. Vercel shows you DNS records to add

### 4.2 Update DNS at your registrar
Log in to wherever you bought your domain (GoDaddy, Namecheap, Google Domains, etc.)
and add the records Vercel gave you. Typically:

- **A record**: `@` → Vercel's IP
- **CNAME record**: `www` → `cname.vercel-dns.com`

DNS propagation takes 5 minutes to 24 hours (usually under 30 minutes).

### 4.3 SSL is automatic
Vercel provisions a free SSL certificate (HTTPS) automatically once DNS resolves.
No configuration needed.

---

## Phase 5 — Plug In Your Supabase Keys

Open `login.html` and find the config block near the bottom:

```js
const SUPABASE_URL  = 'YOUR_SUPABASE_PROJECT_URL';
const SUPABASE_ANON = 'YOUR_SUPABASE_ANON_KEY';
```

Replace with your actual values. Do the same in the AUTH GUARD block in each
dashboard page (index.html, curves.html, inventory.html, margins.html, news.html, x_feed.html).

The auth guard block to add to each page looks like this — paste it right
after `<meta charset="UTF-8">` in the `<head>`:

```html
<script src="https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2"></script>
<script>
(function() {
  var SUPABASE_URL  = 'https://yourproject.supabase.co';
  var SUPABASE_ANON = 'eyJ...your-anon-key...';
  var sb = supabase.createClient(SUPABASE_URL, SUPABASE_ANON);
  sb.auth.getSession().then(function(r) {
    if (!r.data.session) window.location.href = 'login.html';
  });
})();
</script>
```

---

## Phase 6 — Your Ongoing Workflow

Once this is set up, updating your site is:

1. Edit files locally in your EIA Dashboard folder
2. Open GitHub Desktop → you'll see changed files listed
3. Write a short commit message (e.g. "Update WTI chart colors")
4. Click **Commit to main** → **Push origin**
5. Vercel detects the push and auto-deploys in ~30 seconds
6. Changes are live at morningoilbrief.com

No FTP, no manual uploads, no downtime.

---

## Managing Users

In Supabase → **Authentication → Users** you can:
- See every registered user, their email, signup date, last login
- Manually delete users
- Ban users
- Invite specific people by email

---

## Scaling Milestones

| Users | What to consider |
|---|---|
| 0–1,000 | Everything free. No action needed. |
| 1,000–10,000 | Still on free tiers for both Supabase and Vercel |
| 10,000+ | Supabase Pro ($25/mo), Vercel Pro ($20/mo) — both very reasonable |
| Paid subscriptions | Add Stripe + Supabase Row Level Security to gate premium content |

---

## Security Checklist Before Going Live

- [ ] Supabase Site URL set to your production domain
- [ ] Redirect URLs configured in Supabase
- [ ] Auth guard added to every dashboard HTML page
- [ ] GitHub repo is set to **Private**
- [ ] HTTPS working (green padlock in browser)
- [ ] Test signup, email confirmation, and login end-to-end
- [ ] Test that going directly to index.html without logging in redirects to login.html
