# SignalForge

## Product vision

SignalForge is an evidence-first competitive intelligence platform for product, strategy, and go-to-market teams. It continuously watches public competitor signals—product pages, pricing, documentation, changelogs, job boards, press releases, news article regarding them—then turns noisy changes into cited, reviewable business signals.

The product is not “an LLM that summarizes websites.” It is a reliable monitoring workflow that answers:

- What changed?
- When did it change?
- Where is the evidence?
- Why might it matter to our business?
- How confident are we, and does a human need to review it?

The original daily crawler and Monday Slack briefing for the whole business team remain core capabilities, but the product should feel like a useful intelligence system rather than a scheduled script. Users can inspect a signal in a dashboard, open the exact source snapshot, correct a classification, and build a defensible weekly narrative.

## Target users

### Primary user: product and strategy manager

Works at a startup or mid-market company, tracks 3–15 competitors, and needs a fast answer for roadmap, positioning, and pricing decisions. They do not have time to manually check every source and do not trust uncited AI summaries.

### Secondary users

- Product marketing managers monitoring positioning, packaging, and messaging.
- Sales enablement teams looking for evidence-backed competitor updates.
- Founders and executives who need a concise weekly market briefing.
- Analysts who want structured events and exportable evidence, not another dashboard full of charts.

## Core user journey

1. A user creates a workspace and adds competitors plus public sources to monitor.
2. SignalForge validates the source type, crawl policy, and schedule.
3. A scheduled run fetches permitted content and stores a normalized snapshot.
4. The system compares the snapshot with the last successful snapshot using stable content hashes and semantic sections.
5. Changed sections become candidate events.
6. A LangGraph workflow classifies each event, extracts structured facts, assigns impact and confidence, and attaches source evidence.
7. Low-confidence or high-impact events enter a human review queue.
8. Approved events appear in the timeline and are included in the weekly briefing.
9. The Monday briefing is published to a configured Slack channel, links every material claim to its evidence, and clearly labels inference versus observed fact.

## MVP scope

The first release should solve one narrow problem exceptionally well:

- Multi-tenant workspaces with members and roles.
- Competitor profiles and manually configured public sources.
- Daily scheduled crawling with retries and run history.
- HTML and RSS ingestion; Playwright only where JavaScript rendering is necessary.
- Hash- and section-based change detection.
- Event categories: pricing, product/feature, positioning, hiring, partnership, funding, and company/news.
- Structured LLM classification with confidence and evidence spans.
- Review queue for uncertain or high-impact events.
- Timeline filtered by competitor, category, impact, and date.
- Weekly Slack briefing in a shared business channel with source links and per-event citations.
- Slack deep links from each briefing item to the SignalForge event and source evidence.
- Basic audit log and operational status page.

## Deliberate non-goals for MVP

- Private/authenticated competitor data or bypassing access controls.
- Circumventing robots.txt, rate limits, paywalls, CAPTCHAs, or bot protections.
- Broad social-media scraping.
- Fully autonomous strategic recommendations or trading/investment advice.
- Real-time crawling of every source.
- A generic “chat with your competitors” interface before the evidence workflow is trustworthy.

## What “done” looks like

A portfolio reviewer should be able to:

- Add two competitors and at least three public sources each.
- Run an ingestion job locally and see source/run status.
- Change a fixture page and observe one deduplicated signal.
- Open a signal and see the before/after evidence, timestamp, category, impact, confidence, and source URL.
- Correct a classification and see the correction persist.
- Generate a weekly briefing from approved events.
- Trace a briefing claim back to the event and source snapshot.
- Run the full test suite with deterministic fixtures and no live web dependency.

Operationally, a successful MVP has:

- Idempotent jobs that can be safely retried.
- No unsupported claim without an evidence reference.
- Clear handling for blocked, changed, or unavailable sources.
- Cost and latency visibility for LLM calls.
- A documented data-retention and source-compliance policy.

## Success metrics

### Product metrics

- Time from a source change to a reviewable signal.
- Percentage of material signals with valid evidence.
- Duplicate-signal rate.
- Reviewer acceptance/edit rate.
- Weekly Slack briefing delivery, link-click, and team-engagement rates.

### Engineering metrics

- Successful scheduled-run rate.
- Median and p95 ingestion latency by source type.
- Classification cost per changed source.
- Queue depth and retry rate.
- Test coverage of change detection, deduplication, and workflow recovery.

The north-star outcome is **trusted hours saved per workspace per week**, not the number of pages crawled.

## Portfolio and career value

This project demonstrates skills relevant to backend, platform, applied AI, and product-engineering roles:

- Production API and relational data modeling.
- Background jobs, retries, idempotency, and observability.
- Browser automation used responsibly.
- LLM orchestration with typed outputs and human-in-the-loop review.
- Evaluation of extraction quality rather than demo-only prompting.
- Security, privacy, and multi-tenant boundaries.
- A complete user-facing workflow instead of an isolated AI prototype.
