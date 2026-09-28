import { FormEvent, StrictMode, useEffect, useState, useCallback, createContext, useContext } from "react";
import { createRoot } from "react-dom/client";
import { Auth0Provider, useAuth0 } from "@auth0/auth0-react";

import "./styles.css";

// ---------------------------------------------------------------------------
// Dev-mode auth shim — used when Auth0 env vars are absent.
// Provides the same shape as useAuth0() so App() is identical in both modes.
// ---------------------------------------------------------------------------
const DEV_AUTH0_DOMAIN = import.meta.env.VITE_AUTH0_DOMAIN || "";

const DevAuthContext = createContext({
  isAuthenticated: true,
  loginWithRedirect: async () => {},
  logout: async (_?: object) => {},
  getAccessTokenSilently: async () => "" as string,
  user: { email: "dev@local" },
});

const useAuth = () => {
  if (!DEV_AUTH0_DOMAIN) return useContext(DevAuthContext);
  // eslint-disable-next-line react-hooks/rules-of-hooks
  return useAuth0();
};

type Competitor = {
  id: string;
  name: string;
  canonical_domain: string;
  active: boolean;
};

type Source = {
  id: string;
  competitor_id: string;
  source_type: string;
  normalized_url: string;
  status: string;
};

type CrawlRun = {
  run_id: string;
  source_id: string;
  status: string;
  attempt: number;
  duration_ms: number | null;
  byte_count: number;
  error_code: string | null;
  retryable: boolean;
};

type Event = {
  event_id: string;
  source_id: string;
  title: string;
  category: string;
  impact: string;
  confidence: number;
  status: string;
  observed_facts: string[];
  observed_at: string;
  version: number;
};

type EventDetail = Event & {
  before_snapshot_id: string | null;
  after_snapshot_id: string;
  evidence: { evidence_id: string; quoted_text: string; marker: string; source_url: string }[];
  snapshots: Record<string, { content: string; fetched_url: string; fetched_at: string }>;
  notes: { note_id: string; body: string; author_user_id: string }[];
};

type Briefing = {
  briefing_id: string;
  period_start: string;
  period_end: string;
  summary: string;
  events: {
    event_id: string;
    rank: number;
    title: string;
    observed_facts: string[];
    interpretation: string;
    citations: string[];
  }[];
};

const API_URL = import.meta.env.VITE_API_URL ?? "http://localhost:8000";
const IS_DEV_AUTH = !DEV_AUTH0_DOMAIN;

async function request<T>(path: string, options: RequestInit = {}, token?: string, workspaceId?: string): Promise<T> {
  const reqHeaders: Record<string, string> = {
    "Content-Type": "application/json",
  };
  if (IS_DEV_AUTH) {
    // Development mode: backend uses header-based auth, no token needed
    reqHeaders["X-User-Id"] = "dev-user";
    reqHeaders["X-Role"] = "admin";
    if (workspaceId) reqHeaders["X-Workspace-Id"] = workspaceId;
  } else {
    if (token) reqHeaders["Authorization"] = `Bearer ${token}`;
    if (workspaceId) reqHeaders["X-Workspace-Id"] = workspaceId;
  }

  const response = await fetch(`${API_URL}${path}`, {
    ...options,
    headers: { ...reqHeaders, ...options.headers },
  });
  if (!response.ok) {
    const body = (await response.json()) as { detail?: { message?: string } };
    throw new Error(body.detail?.message ?? "The request failed.");
  }
  return (await response.json()) as T;
}

function App() {
  const [competitors, setCompetitors] = useState<Competitor[]>([]);
  const [sources, setSources] = useState<Source[]>([]);
  const [name, setName] = useState("");
  const [domain, setDomain] = useState("");
  const [sourceUrl, setSourceUrl] = useState("");
  const [message, setMessage] = useState("");
  const [runs, setRuns] = useState<CrawlRun[]>([]);
  const [events, setEvents] = useState<Event[]>([]);
  const [selectedEvent, setSelectedEvent] = useState<EventDetail | null>(null);
  const [briefing, setBriefing] = useState<Briefing | null>(null);
  const [weekStart, setWeekStart] = useState("");
  const [channelId, setChannelId] = useState("");
  const [editCategory, setEditCategory] = useState("");
  const [editImpact, setEditImpact] = useState("");
  const { isAuthenticated, loginWithRedirect, logout, getAccessTokenSilently, user } = useAuth();
  const [workspaceId, setWorkspaceId] = useState("workspace-a");
  const [note, setNote] = useState("");
  const [timelineEvents, setTimelineEvents] = useState<Event[]>([]);
  const [timelineCategory, setTimelineCategory] = useState("");
  const [timelineImpact, setTimelineImpact] = useState("");
  const [timelineStatus, setTimelineStatus] = useState("");

  const apiRequest = useCallback(async <T,>(path: string, options: RequestInit = {}) => {
    let token;
    try {
      token = await getAccessTokenSilently();
    } catch {
      // Ignored
    }
    return request<T>(path, options, token, workspaceId);
  }, [getAccessTokenSilently, workspaceId]);

  const loadTimeline = useCallback(async () => {
    if (!isAuthenticated) return;
    const params = new URLSearchParams();
    if (timelineCategory) params.set("category", timelineCategory);
    if (timelineImpact) params.set("impact", timelineImpact);
    if (timelineStatus) params.set("status", timelineStatus);
    const qs = params.toString() ? `?${params.toString()}` : "";
    try {
      const data = await apiRequest<Event[]>(`/v1/workspaces/${workspaceId}/events${qs}`);
      setTimelineEvents(data);
    } catch {
      // Ignored
    }
  }, [isAuthenticated, apiRequest, workspaceId, timelineCategory, timelineImpact, timelineStatus]);

  const loadMonitoringData = useCallback(async () => {
    if (!isAuthenticated) return;
    const [competitorData, sourceData, runData] = await Promise.all([
      apiRequest<Competitor[]>(`/v1/workspaces/${workspaceId}/competitors`),
      apiRequest<Source[]>(`/v1/workspaces/${workspaceId}/sources`),
      apiRequest<CrawlRun[]>(`/v1/workspaces/${workspaceId}/runs`),
    ]);
    setCompetitors(competitorData);
    setSources(sourceData);
    setRuns(runData);
    setEvents(await apiRequest<Event[]>(`/v1/workspaces/${workspaceId}/events?status=needs_review`));
    void loadTimeline();
  }, [isAuthenticated, apiRequest, workspaceId, loadTimeline]);

  const openEvent = async (eventId: string) => {
    try {
      setSelectedEvent(await apiRequest<EventDetail>(`/v1/workspaces/${workspaceId}/events/${eventId}`));
    } catch (error) {
      setMessage((error as Error).message);
    }
  };

  const review = async (action: "approve" | "reject") => {
    if (!selectedEvent) return;
    try {
      await apiRequest(`/v1/workspaces/${workspaceId}/events/${selectedEvent.event_id}/review`, {
        method: "POST",
        body: JSON.stringify({ action, expected_version: selectedEvent.version }),
      });
      setMessage(`Event ${action}d.`);
      setSelectedEvent(null);
      await loadMonitoringData();
    } catch (error) {
      setMessage((error as Error).message);
    }
  };

  const editClassification = async () => {
    if (!selectedEvent) return;
    try {
      await apiRequest(`/v1/workspaces/${workspaceId}/events/${selectedEvent.event_id}/review`, {
        method: "POST",
        body: JSON.stringify({
          action: "edit_classification",
          expected_version: selectedEvent.version,
          category: editCategory || selectedEvent.category,
          impact: editImpact || selectedEvent.impact,
        }),
      });
      setMessage("Classification updated.");
      setSelectedEvent(null);
      await loadMonitoringData();
    } catch (error) {
      setMessage((error as Error).message);
    }
  };

  const addNote = async () => {
    if (!selectedEvent || !note.trim()) return;
    try {
      await apiRequest(`/v1/workspaces/${workspaceId}/events/${selectedEvent.event_id}/review`, {
        method: "POST",
        body: JSON.stringify({ action: "add_note", expected_version: selectedEvent.version, note }),
      });
      setNote("");
      setMessage("Note added.");
      setSelectedEvent(null);
    } catch (error) {
      setMessage((error as Error).message);
    }
  };

  const previewBriefing = async () => {
    try {
      const data = await apiRequest<Briefing>(`/v1/workspaces/${workspaceId}/briefings/preview`, {
        method: "POST",
        body: JSON.stringify(weekStart ? { period_start: weekStart } : {}),
      });
      setBriefing(data);
    } catch (error) {
      setMessage((error as Error).message);
    }
  };

  const connectSlack = async (event: FormEvent) => {
    event.preventDefault();
    try {
      await apiRequest(`/v1/workspaces/${workspaceId}/slack/connect`, {
        method: "POST",
        body: JSON.stringify({ team_id: "demo-team", token_reference: "env:SLACK_BOT_TOKEN" }),
      });
      await apiRequest(`/v1/workspaces/${workspaceId}/slack/destinations`, {
        method: "POST",
        body: JSON.stringify({ channel_id: channelId, channel_name: channelId }),
      });
      setMessage("Slack destination configured.");
    } catch (error) {
      setMessage((error as Error).message);
    }
  };

  const publishBriefing = async () => {
    if (!briefing) return;
    try {
      const result = await apiRequest<{ status: string }>(
        `/v1/workspaces/${workspaceId}/briefings/${briefing.briefing_id}/publish-to-slack`,
        { method: "POST", headers: { "Idempotency-Key": `briefing:${briefing.briefing_id}` } },
      );
      setMessage(`Slack delivery ${result.status}.`);
    } catch (error) {
      setMessage((error as Error).message);
    }
  };

  useEffect(() => {
    void loadMonitoringData().catch((error: Error) => setMessage(error.message));
  }, [loadMonitoringData]);

  useEffect(() => {
    if (!isAuthenticated) return;
    const interval = window.setInterval(() => {
      void loadMonitoringData();
    }, 4000);
    return () => window.clearInterval(interval);
  }, [isAuthenticated, loadMonitoringData]);

  const submitCompetitor = async (event: FormEvent) => {
    event.preventDefault();
    try {
      await apiRequest(`/v1/workspaces/${workspaceId}/competitors`, {
        method: "POST",
        body: JSON.stringify({ name, canonical_domain: domain }),
      });
      setName("");
      setDomain("");
      setMessage("Competitor added.");
      await loadMonitoringData();
    } catch (error) {
      setMessage((error as Error).message);
    }
  };

  const submitSource = async (event: FormEvent) => {
    event.preventDefault();
    const competitor = competitors[0];
    if (!competitor) {
      setMessage("Add a competitor before adding a source.");
      return;
    }
    try {
      await apiRequest(`/v1/workspaces/${workspaceId}/sources`, {
        method: "POST",
        body: JSON.stringify({
          competitor_id: competitor.id,
          source_type: "html",
          url: sourceUrl,
        }),
      });
      setSourceUrl("");
      setMessage("Source added.");
      await loadMonitoringData();
    } catch (error) {
      setMessage((error as Error).message);
    }
  };

  const runSource = async (sourceId: string) => {
    try {
      await apiRequest(`/v1/workspaces/${workspaceId}/sources/${sourceId}/crawl`, {
        method: "POST",
        headers: { "Idempotency-Key": `manual:${sourceId}:${crypto.randomUUID()}` },
      });
      setMessage("Crawl queued. Run history will update automatically.");
      await loadMonitoringData();
    } catch (error) {
      setMessage((error as Error).message);
    }
  };

  return (
    <main className="shell">
      <p className="eyebrow">SIGNALFORGE</p>
      <h1>Competitive intelligence your business can verify.</h1>
      <p className="lede">
        Public-source changes, evidence-backed signals, and a Monday briefing delivered to the
        business team in Slack.
      </p>
      
      {!isAuthenticated ? (
        <section className="dashboard" style={{ textAlign: "center", padding: "4rem" }}>
          <h2>Please log in to continue</h2>
          <button type="button" onClick={() => void loginWithRedirect()}>Log in</button>
        </section>
      ) : (
        <>
          <div className="workspace-selector" style={{ marginBottom: "2rem", display: "flex", gap: "1rem", alignItems: "center" }}>
            <label>Current Workspace: <input value={workspaceId} onChange={(e) => setWorkspaceId(e.target.value)} /></label>
            <span>Logged in as {user?.email}</span>
            <button type="button" onClick={() => void logout({ logoutParams: { returnTo: window.location.origin } })}>Log out</button>
          </div>
          
          <section className="dashboard" aria-label="Monitoring configuration">
        <div className="section-heading">
          <div>
            <p className="eyebrow">MONITORING SETUP</p>
            <h2>Competitors and sources</h2>
          </div>
          <span className="status-card"><span className="status-dot" aria-hidden="true" />Connected</span>
        </div>
        <div className="forms">
          <form onSubmit={submitCompetitor}>
            <h3>Add competitor</h3>
            <label>Name<input value={name} onChange={(event) => setName(event.target.value)} required /></label>
            <label>Canonical domain<input value={domain} onChange={(event) => setDomain(event.target.value)} placeholder="acme.example" required /></label>
            <button type="submit">Add competitor</button>
          </form>
          <form onSubmit={submitSource}>
            <h3>Add HTML source</h3>
            <label>Public URL<input type="url" value={sourceUrl} onChange={(event) => setSourceUrl(event.target.value)} placeholder="https://acme.example/pricing" required /></label>
            <p className="hint">The source is attached to the first configured competitor in this demo workspace.</p>
            <button type="submit">Add source</button>
          </form>
        </div>
        <p className="message" role="status">{message}</p>
        <div className="lists">
          <div><h3>Competitors ({competitors.length})</h3>{competitors.map((item) => <p className="list-item" key={item.id}>{item.name}<span>{item.canonical_domain}</span></p>)}</div>
          <div><h3>Sources ({sources.length})</h3>{sources.map((item) => <p className="list-item" key={item.id}><span className="list-main">{item.normalized_url}<small>{item.status}</small></span><button className="small-button" type="button" onClick={() => void runSource(item.id)}>Run now</button></p>)}</div>
        </div>
        <div className="run-history">
          <h3>Run history ({runs.length})</h3>
          {runs.length === 0 ? <p className="hint">No crawl runs yet.</p> : runs.slice(-6).reverse().map((run) => <p className="list-item" key={run.run_id}><span className="list-main">{run.run_id.slice(0, 8)}<small>attempt {run.attempt} · {run.byte_count} bytes</small></span><span className={`run-status ${run.status}`}>{run.status}</span></p>)}
        </div>
      </section>
      <section className="dashboard" aria-label="Review queue">
        <div className="section-heading"><div><p className="eyebrow">HUMAN REVIEW</p><h2>Needs review ({events.length})</h2></div></div>
        {events.length === 0 ? <p className="hint">No uncertain or high-impact events are waiting.</p> : events.map((event) => (
          <button className="review-item" type="button" key={event.event_id} onClick={() => void openEvent(event.event_id)}>
            <span><strong>{event.title}</strong><small>{event.category} · {event.impact} impact · {Math.round(event.confidence * 100)}% confidence</small></span>
            <span>Review</span>
          </button>
        ))}
        {selectedEvent && <div className="event-detail">
          <h3>{selectedEvent.title}</h3>
          <p>{selectedEvent.observed_facts.join(" ")}</p>
          <div className="comparison"><div><strong>Before</strong><pre>{selectedEvent.before_snapshot_id ? selectedEvent.snapshots[selectedEvent.before_snapshot_id]?.content : "No previous snapshot"}</pre></div><div><strong>After</strong><pre>{selectedEvent.snapshots[selectedEvent.after_snapshot_id]?.content}</pre></div></div>
          <div className="evidence-grid">{selectedEvent.evidence.map((item) => <blockquote key={item.evidence_id}><small>{item.marker}</small>{item.quoted_text}<a href={item.source_url} target="_blank" rel="noreferrer">Source evidence</a></blockquote>)}</div>
          <div className="actions"><button type="button" onClick={() => void review("approve")}>Approve</button><button type="button" onClick={() => void review("reject")}>Reject</button></div>
          <div className="actions"><label>Category<select value={editCategory || selectedEvent.category} onChange={(event) => setEditCategory(event.target.value)}><option value="pricing">Pricing</option><option value="product_feature">Product/feature</option><option value="positioning">Positioning</option><option value="hiring">Hiring</option><option value="partnership">Partnership</option><option value="funding">Funding</option><option value="company_news">Company/news</option></select></label><label>Impact<select value={editImpact || selectedEvent.impact} onChange={(event) => setEditImpact(event.target.value)}><option value="low">Low</option><option value="medium">Medium</option><option value="high">High</option></select></label><button type="button" onClick={() => void editClassification()}>Save classification</button></div>
          <div className="actions"><label>Reviewer note<input value={note} onChange={(event) => setNote(event.target.value)} placeholder="Add context for the team" /></label><button type="button" onClick={() => void addNote()}>Add note</button></div>
        </div>}
      </section>
      <section className="dashboard" aria-label="Event timeline">
        <div className="section-heading">
          <div>
            <p className="eyebrow">INTELLIGENCE TIMELINE</p>
            <h2>All events ({timelineEvents.length})</h2>
          </div>
        </div>
        <div className="actions" style={{ flexWrap: "wrap" }}>
          <label>
            Category
            <select value={timelineCategory} onChange={(event) => setTimelineCategory(event.target.value)}>
              <option value="">All categories</option>
              <option value="pricing">Pricing</option>
              <option value="product_feature">Product/feature</option>
              <option value="positioning">Positioning</option>
              <option value="hiring">Hiring</option>
              <option value="partnership">Partnership</option>
              <option value="funding">Funding</option>
              <option value="company_news">Company/news</option>
            </select>
          </label>
          <label>
            Impact
            <select value={timelineImpact} onChange={(event) => setTimelineImpact(event.target.value)}>
              <option value="">All impacts</option>
              <option value="low">Low</option>
              <option value="medium">Medium</option>
              <option value="high">High</option>
            </select>
          </label>
          <label>
            Status
            <select value={timelineStatus} onChange={(event) => setTimelineStatus(event.target.value)}>
              <option value="">All statuses</option>
              <option value="approved">Approved</option>
              <option value="needs_review">Needs review</option>
              <option value="candidate">Candidate</option>
              <option value="rejected">Rejected</option>
            </select>
          </label>
        </div>
        {timelineEvents.length === 0 ? (
          <p className="hint">No events match the selected filters.</p>
        ) : (
          <div className="lists" style={{ gridTemplateColumns: "1fr" }}>
            {timelineEvents.map((evt) => (
              <div
                className="list-item"
                key={evt.event_id}
                style={{ alignItems: "center" }}
              >
                <span className="list-main">
                  <strong>{evt.title}</strong>
                  <small>
                    {evt.category} · {evt.impact} impact · {Math.round(evt.confidence * 100)}% confidence
                    {evt.observed_at ? ` · ${evt.observed_at.slice(0, 10)}` : ""}
                  </small>
                </span>
                <span style={{ display: "flex", gap: "0.5rem", alignItems: "center" }}>
                  <span className={`run-status ${evt.status === "approved" ? "succeeded" : evt.status === "rejected" ? "failed" : ""}`}>
                    {evt.status}
                  </span>
                  <button className="small-button" type="button" onClick={() => void openEvent(evt.event_id)}>
                    Details
                  </button>
                </span>
              </div>
            ))}
          </div>
        )}
      </section>
      <section className="dashboard" aria-label="Weekly briefing">
        <div className="section-heading"><div><p className="eyebrow">WEEKLY BRIEFING</p><h2>Preview and publish</h2></div></div>
          <div className="briefing-controls"><label>Week starting (Monday)<input type="date" value={weekStart} onChange={(event) => setWeekStart(event.target.value)} /></label><button type="button" onClick={() => void previewBriefing()}>Preview approved events</button></div>
        {briefing && <div className="briefing-preview"><p><strong>{briefing.summary}</strong></p>{briefing.events.map((item) => <article key={item.event_id}><h3>{item.rank}. {item.title}</h3><p><strong>Observed:</strong> {item.observed_facts.join("; ")}</p><p><strong>Interpretation:</strong> {item.interpretation}</p>{item.citations.map((citation) => <a href={citation} key={citation} target="_blank" rel="noreferrer">Evidence link</a>)}</article>)}<form onSubmit={connectSlack} className="slack-form"><label>Destination channel<input value={channelId} onChange={(event) => setChannelId(event.target.value)} placeholder="C0123456789" required /></label><button type="submit">Connect Slack</button><button type="button" onClick={() => void publishBriefing()}>Publish to Slack</button></form></div>}
      </section>
        </>
      )}
    </main>
  );
}

const AUTH0_DOMAIN = import.meta.env.VITE_AUTH0_DOMAIN || "";
const AUTH0_CLIENT_ID = import.meta.env.VITE_AUTH0_CLIENT_ID || "";
const AUTH0_AUDIENCE = import.meta.env.VITE_AUTH0_AUDIENCE || "";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    {DEV_AUTH0_DOMAIN ? (
      <Auth0Provider
        domain={DEV_AUTH0_DOMAIN}
        clientId={AUTH0_CLIENT_ID}
        authorizationParams={{
          redirect_uri: window.location.origin,
          audience: AUTH0_AUDIENCE,
        }}
      >
        <App />
      </Auth0Provider>
    ) : (
      // Dev mode: no Auth0 config, use shim so the app works out of the box
      <DevAuthContext.Provider value={{
        isAuthenticated: true,
        loginWithRedirect: async () => {},
        logout: async (_?: object) => {},
        getAccessTokenSilently: async () => "",
        user: { email: "dev@local" },
      }}>
        <App />
      </DevAuthContext.Provider>
    )}
  </StrictMode>,
);
