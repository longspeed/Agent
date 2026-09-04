import { useEffect, useState } from 'react'
import { NavBar } from './components/NavBar'
import { useAnimatedCount } from './hooks/useAnimatedCount'

const PUBLIC_BOOKING_URL = (import.meta.env.VITE_PUBLIC_BOOKING_URL || '').trim()

interface AccountSettings {
  sender_name: string | null
  sender_company: string | null
  meeting_purpose: string | null
  custom_instructions: string | null
  calendar_booking_link: string | null
  notify_email: string | null
  google_sheet_id: string | null
  outreach_send_mode: 'manual' | 'auto' | null
  follow_up_delay_days: number | null
}

interface MeResponse {
  email: string
  googleConnected: boolean
  googleCapabilities: {
    monitor: boolean
    send: boolean
    sheets: boolean
  }
  autoSendEnabled: boolean
  settings: AccountSettings
  onboarded: boolean
  sendBlockers: string[]
  monitoring: {
    status: 'not_connected' | 'waiting' | 'healthy' | 'attention' | 'stale'
    message: string
    last_checked_at: string | null
  }
}

interface UsageSummary {
  [kind: string]: { events: number; quantity: number }
}

interface PlanInfo {
  plan: 'trial' | 'pilot' | 'team'
  name: string
  price_monthly_usd: number
  daily_send_limit: number
  lead_searches_per_hour: number
  monthly_drafts: number | null
  lead_allowance: number | null
  usage: {
    drafts_used: number
    drafts_remaining: number | null
    leads_used: number
    leads_remaining: number | null
  }
  checkout_available: boolean
}

interface SheetOption {
  id: string
  name: string
}

const EMPTY_FORM: AccountSettings = {
  sender_name: '',
  sender_company: '',
  meeting_purpose: '',
  custom_instructions: '',
  calendar_booking_link: '',
  notify_email: '',
  google_sheet_id: '',
  outreach_send_mode: 'manual',
  follow_up_delay_days: 3,
}

function CheckIcon() {
  return (
    <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="#FFFFFF" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round">
      <path d="M20 6L9 17l-5-5" />
    </svg>
  )
}

function StepDot({ done }: { done: boolean }) {
  return (
    <span
      className="w-[18px] h-[18px] rounded-full flex-none flex items-center justify-center border-[1.5px] transition-all duration-250"
      style={{
        borderColor: done ? 'var(--color-ok)' : 'var(--color-line-hover)',
        background: done ? 'var(--color-ok)' : 'transparent',
        transform: done ? 'scale(1.05)' : 'scale(1)',
      }}
    >
      <span className="transition-opacity duration-200" style={{ opacity: done ? 1 : 0 }}>
        <CheckIcon />
      </span>
    </span>
  )
}

function StatTile({ label, value }: { label: string; value: number }) {
  const animated = useAnimatedCount(value)
  return (
    <div className="rounded-xl p-4 bg-floating border border-line">
      <div className="font-display text-2xl" style={{ letterSpacing: '-0.02em' }}>
        {animated.toLocaleString('en-US')}
      </div>
      <div className="text-xs mt-1 text-sand">{label}</div>
    </div>
  )
}

interface DeliverabilityFinding {
  name: string
  status: 'ok' | 'missing' | 'warning' | 'unknown'
  detail: string
}

interface DeliverabilityResponse {
  address: string
  domain: string
  managed: boolean
  status: 'not_connected' | 'ready' | 'blocked' | 'unknown'
  summary: string
  findings: DeliverabilityFinding[]
  send_blockers: string[]
}

function MonitoringStatus({ monitoring }: { monitoring: MeResponse['monitoring'] }) {
  const healthy = monitoring.status === 'healthy'
  const waiting = monitoring.status === 'waiting'
  const connected = healthy || waiting
  const tone = healthy ? 'var(--color-ok)' : waiting ? 'var(--color-gold)' : 'var(--color-accent)'
  const label = healthy
    ? 'Gmail Sent monitoring active'
    : waiting
      ? 'Gmail Sent monitoring starting'
      : monitoring.status === 'not_connected'
        ? 'Gmail Sent monitoring not connected'
        : 'Gmail Sent monitoring needs attention'

  return (
    <div
      className="mt-4 rounded-lg px-4 py-3 text-sm border border-line"
      style={{ background: connected ? 'var(--color-floating)' : 'rgba(194,73,29,0.10)', color: tone }}
      role={connected ? 'status' : 'alert'}
    >
      <div className="font-medium">{label}</div>
      <div className="mt-1 text-xs text-sand">{monitoring.message}</div>
      {monitoring.last_checked_at && (
        <div className="mt-1 text-xs text-mute">
          Last worker check: {new Date(monitoring.last_checked_at).toLocaleString()}
        </div>
      )}
    </div>
  )
}

function DomainSafety({ report }: { report: DeliverabilityResponse }) {
  const tone = report.status === 'ready' ? 'var(--color-ok)' : report.status === 'not_connected' ? 'var(--color-mute)' : 'var(--color-accent)'
  return (
    <section className="mt-4 rounded-xl border border-line bg-floating p-4" aria-labelledby="domain-safety-title">
      <div className="flex items-start justify-between gap-3 flex-wrap">
        <div>
          <h3 id="domain-safety-title" className="text-sm font-medium text-cream">Domain safety check</h3>
          <p className="mt-1 text-xs text-sand">{report.address ? `Sending as ${report.address}.` : report.summary}</p>
        </div>
        <span className="text-xs font-medium" style={{ color: tone }}>
          {report.status === 'ready' ? 'Ready for optional first-touch' : report.status === 'blocked' ? 'First-touch paused' : report.status === 'unknown' ? 'Check required' : 'Not connected'}
        </span>
      </div>
      {report.findings.length > 0 && (
        <div className="mt-3 grid grid-cols-1 sm:grid-cols-3 gap-2">
          {report.findings.map((finding) => (
            <div key={finding.name} className="rounded-lg border border-line px-3 py-2">
              <div className="flex items-center justify-between gap-2 text-xs">
                <span className="font-medium text-cream">{finding.name}</span>
                <span style={{ color: finding.status === 'ok' ? 'var(--color-ok)' : 'var(--color-accent)' }}>{finding.status}</span>
              </div>
              <p className="mt-1 text-[11px] leading-relaxed text-mute">{finding.detail}</p>
            </div>
          ))}
        </div>
      )}
      <p className="mt-3 text-xs leading-relaxed text-sand">
        Optional first-touch sending pauses when SPF/DMARC is missing, a DKIM key is revoked, or DNS cannot be checked. Gmail Sent monitoring remains available while you fix it. This is an authentication gate, not an inbox-placement guarantee.
      </p>
    </section>
  )
}

export function SettingsPage() {
  const [me, setMe] = useState<MeResponse | null>(null)
  const [usage, setUsage] = useState<UsageSummary>({})
  const [plan, setPlan] = useState<PlanInfo | null>(null)
  const [form, setForm] = useState<AccountSettings>(EMPTY_FORM)
  const [saveStatus, setSaveStatus] = useState('')
  const [flash, setFlash] = useState<{ text: string; ok: boolean } | null>(null)
  const [loadError, setLoadError] = useState('')
  const [billingBusy, setBillingBusy] = useState(false)
  const [exportBusy, setExportBusy] = useState(false)
  const [testAlertBusy, setTestAlertBusy] = useState(false)
  const [testAlertStatus, setTestAlertStatus] = useState('')
  const [deliverability, setDeliverability] = useState<DeliverabilityResponse | null>(null)

  const [sheetPickerOpen, setSheetPickerOpen] = useState(false)
  const [sheets, setSheets] = useState<SheetOption[]>([])
  const [sheetPickerStatus, setSheetPickerStatus] = useState('')
  const [manualSheetVisible, setManualSheetVisible] = useState(false)

  async function load() {
    const meResponse = await fetch('/api/me')
    if (meResponse.status === 401) {
      const next = `${window.location.pathname}${window.location.search}`
      window.location.replace(`/login?next=${encodeURIComponent(next)}`)
      return null
    }
    if (!meResponse.ok) throw new Error('Could not load your account.')
    const meRes: MeResponse = await meResponse.json()
    setMe(meRes)
    setForm({
      sender_name: meRes.settings.sender_name || '',
      sender_company: meRes.settings.sender_company || '',
      meeting_purpose: meRes.settings.meeting_purpose || '',
      custom_instructions: meRes.settings.custom_instructions || '',
      calendar_booking_link: meRes.settings.calendar_booking_link || '',
      notify_email: meRes.settings.notify_email || '',
      google_sheet_id: meRes.settings.google_sheet_id || '',
      outreach_send_mode: meRes.autoSendEnabled ? (meRes.settings.outreach_send_mode || 'manual') : 'manual',
      follow_up_delay_days: meRes.settings.follow_up_delay_days || 3,
    })

    // usage and plan don't depend on meRes -- fetch both concurrently
    // instead of awaiting three requests in sequence.
    const [usageRes, planRes, deliverabilityRes] = await Promise.all([
      fetch('/api/usage').then((r) => r.json()),
      fetch('/api/plan').then((r) => r.json()),
      fetch('/api/deliverability').then((r) => r.json()),
    ])
    setUsage(usageRes)
    setPlan(planRes)
    setDeliverability(deliverabilityRes)
    return meRes
  }

  useEffect(() => {
    load().catch(() => {
      const message = 'Could not load settings. Refresh to try again.'
      setLoadError(message)
      setFlash({ text: message, ok: false })
    })

    const params = new URLSearchParams(window.location.search)
    if (params.get('connected')) {
      const capability = params.get('connected')
      const labels: Record<string, string> = { monitor: 'Gmail monitoring', send: 'Gmail sending', sheets: 'Google Sheets' }
      setFlash({ text: `${labels[capability || ''] || 'Google access'} authorized.`, ok: true })
      if (capability === 'send') {
        const returnTo = sessionStorage.getItem('sendkeep:return-after-google-grant') || ''
        if (returnTo.startsWith('/outreach')) {
          sessionStorage.removeItem('sendkeep:return-after-google-grant')
          window.location.replace(returnTo)
          return
        }
      }
    }
    if (params.get('google_error')) setFlash({ text: `Google connection failed: ${params.get('google_error')}`, ok: false })
    if (params.get('connected') || params.get('google_error')) {
      history.replaceState(null, '', '/settings')
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  async function handleSave(e: React.FormEvent) {
    e.preventDefault()
    const body = {
      sender_name: form.sender_name?.trim() ?? '',
      sender_company: form.sender_company?.trim() ?? '',
      meeting_purpose: form.meeting_purpose?.trim() ?? '',
      custom_instructions: form.custom_instructions?.trim() ?? '',
      calendar_booking_link: form.calendar_booking_link?.trim() ?? '',
      notify_email: form.notify_email?.trim() ?? '',
      google_sheet_id: form.google_sheet_id?.trim() ?? '',
      outreach_send_mode: form.outreach_send_mode || 'manual',
      follow_up_delay_days: form.follow_up_delay_days || 3,
    }
    const res = await fetch('/api/settings', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
    if (res.ok) {
      // Confirm the persisted write immediately. The follow-up refresh also
      // fetches DNS state, so a slow or unavailable check must not make a
      // successful settings save look like it disappeared.
      setSaveStatus('Saved.')
      setTimeout(() => setSaveStatus(''), 2500)
      try {
        await load()
      } catch {
        setFlash({ text: 'Saved, but the refreshed settings are unavailable. Refresh to verify.', ok: false })
      }
      // The blockers banner above the form (rendered from me.sendBlockers)
      // already carries this message persistently -- repeating it here too
      // showed the same sentence twice on screen with no way to dismiss
      // either copy.
    } else {
      setSaveStatus('Save failed — try again.')
      setTimeout(() => setSaveStatus(''), 2500)
    }
  }

  async function loadSheetsList() {
    if (!me?.googleCapabilities.sheets) {
      setSheetPickerStatus('Authorize optional Sheets & Drive before browsing your Sheets.')
      setSheets([])
      return
    }
    setSheetPickerStatus('Loading your Google Sheets…')
    setSheets([])
    try {
      const res = await fetch('/api/google/sheets')
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        setSheetPickerStatus(data.detail || 'Could not load your sheets — try pasting the ID manually.')
        return
      }
      const list: SheetOption[] = await res.json()
      setSheets(list)
      setSheetPickerStatus(
        list.length ? `${list.length} spreadsheet${list.length === 1 ? '' : 's'} found — pick one:` : 'No spreadsheets found in your Google Drive.',
      )
    } catch {
      setSheetPickerStatus('Could not load your sheets — try pasting the ID manually.')
    }
  }

  async function toggleSheetPicker() {
    const opening = !sheetPickerOpen
    setSheetPickerOpen(opening)
    if (opening) await loadSheetsList()
  }

  async function pickSheet(sheet: SheetOption) {
    setForm((f) => ({ ...f, google_sheet_id: sheet.id }))
    setSheetPickerOpen(false)
    const res = await fetch('/api/settings', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ google_sheet_id: sheet.id }),
    })
    setFlash({ text: res.ok ? `Lead sheet set to "${sheet.name}".` : 'Could not save — try again.', ok: res.ok })
    if (res.ok) load()
  }

  async function disconnectGoogle() {
    await fetch('/api/google/disconnect', { method: 'POST' })
    load()
  }

  async function sendTestAlert() {
    setTestAlertBusy(true)
    setTestAlertStatus('Sending…')
    try {
      const res = await fetch('/api/alerts/test', { method: 'POST' })
      const payload = await res.json().catch(() => ({}))
      if (!res.ok) throw new Error(payload.detail || 'The test alert could not be sent.')
      setTestAlertStatus(`Sent to ${payload.recipient || me?.email}.`)
    } catch (error) {
      setTestAlertStatus(error instanceof Error ? error.message : 'The test alert could not be sent.')
    } finally {
      setTestAlertBusy(false)
    }
  }

  async function exportAccountData() {
    setExportBusy(true)
    try {
      const response = await fetch('/api/account/export')
      if (!response.ok) throw new Error('Could not prepare your export. Try again.')
      const blob = await response.blob()
      const url = URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = url
      link.download = 'sendkeep-account-export.json'
      document.body.appendChild(link)
      link.click()
      link.remove()
      URL.revokeObjectURL(url)
      setFlash({ text: 'Export downloaded. It contains Sendkeep records, never OAuth credentials.', ok: true })
    } catch (error) {
      setFlash({ text: error instanceof Error ? error.message : 'Could not prepare your export.', ok: false })
    } finally {
      setExportBusy(false)
    }
  }

  async function startCheckout(planName: 'pilot') {
    if (billingBusy) return
    setBillingBusy(true)
    try {
      const res = await fetch('/api/billing/checkout', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ plan: planName, yearly: false }),
      })
      const data = await res.json().catch(() => ({}))
      if (!res.ok || !data.url) {
        setFlash({ text: data.detail || 'Checkout could not be started. Try again.', ok: false })
        return
      }
      window.location.assign(data.url)
    } catch {
      setFlash({ text: 'Checkout could not be started. Try again.', ok: false })
    } finally {
      setBillingBusy(false)
    }
  }

  if (!me) {
    return (
      <div className="min-h-screen antialiased">
        <NavBar breadcrumb="Settings" />
        <main className="relative max-w-3xl mx-auto px-6 py-10 text-sm text-sand">
          {loadError ? (
            <section className="rounded-xl border border-line bg-floating p-5" role="alert">
              <h1 className="font-display text-xl text-cream">Settings are unavailable</h1>
              <p className="mt-2 text-sand">{loadError}</p>
              <button type="button" className="mt-4 rounded-lg bg-cream px-4 py-2 text-sm text-ink" onClick={() => window.location.reload()}>
                Refresh settings
              </button>
            </section>
          ) : 'Loading…'}
        </main>
      </div>
    )
  }

  const sheetLabel = form.google_sheet_id
    ? sheets.find((s) => s.id === form.google_sheet_id)?.name || `Sheet ID: ${form.google_sheet_id}`
    : 'No sheet selected'
  const sheetOk = !!form.google_sheet_id?.trim()
  const calOk = !!form.calendar_booking_link?.trim()

  return (
    <div className="min-h-screen antialiased">
      <div className="fixed inset-0 noise pointer-events-none" />
      <div
        className="fixed inset-0 pointer-events-none"
        style={{
          background:
            'radial-gradient(ellipse 800px 500px at 15% -10%, rgba(194,73,29,0.08), transparent), radial-gradient(ellipse 600px 400px at 90% 10%, rgba(82,113,132,0.07), transparent)',
        }}
      />
      <NavBar breadcrumb="Settings" />

      <main className="relative max-w-3xl mx-auto px-6 py-10">
        <h1 className="font-display text-2xl mb-1" style={{ letterSpacing: '-0.02em' }}>
          Settings
        </h1>
        <p className="text-sm mb-8 text-sand">
          Signed in as <span className="text-cream">{me.email}</span>
        </p>

        {flash && (
          <div
            className="rounded-lg px-4 py-3 text-sm mb-6 border border-line"
            style={{ background: flash.ok ? 'rgba(57,113,82,0.12)' : 'rgba(168,50,50,0.10)', color: flash.ok ? 'var(--color-ok)' : '#A83232' }}
          >
            {flash.text}
          </div>
        )}

        {!me.onboarded && (
          <section className="rounded-2xl p-6 card-shadow mb-6 bg-elevated border border-line">
            <h2 className="font-display text-lg mb-4" style={{ letterSpacing: '-0.01em' }}>
              Finish setting up
            </h2>
            <ol className="space-y-2 text-sm text-sand">
              <li className="flex items-center gap-2.5" style={{ color: me.googleConnected ? 'var(--color-mute)' : undefined, textDecoration: me.googleConnected ? 'line-through' : 'none' }}>
                <StepDot done={me.googleConnected} />
                Connect your Gmail account
              </li>
              <li className="flex items-center gap-2.5" style={{ color: sheetOk ? 'var(--color-mute)' : undefined, textDecoration: sheetOk ? 'line-through' : 'none' }}>
                <StepDot done={sheetOk} />
                Pick an optional first-touch Google Sheet
              </li>
              <li className="flex items-center gap-2.5" style={{ color: calOk ? 'var(--color-mute)' : undefined, textDecoration: calOk ? 'line-through' : 'none' }}>
                <StepDot done={calOk} />
                Add an optional calendar link for first-touch drafts
              </li>
            </ol>
            <p className="text-xs mt-4 text-mute">
              First time here?{' '}
              <a
                href="/getting-started"
                className="text-accent underline underline-offset-[3px] decoration-accent/40 rounded-[3px] transition-colors duration-200 ease-spring hover:text-accent-hover hover:decoration-accent-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/40 active:opacity-85"
              >
                Read the getting started guide
              </a>{' '}
              for a walkthrough of every step.
            </p>
          </section>
        )}

        <section className="rounded-2xl p-6 card-shadow mb-6 bg-elevated border border-line">
          <div className="flex items-center justify-between gap-4 flex-wrap">
            <div>
              <h2 className="font-display text-lg mb-1" style={{ letterSpacing: '-0.01em' }}>
                Gmail connection
              </h2>
          <p className="text-sm text-sand">{me.googleConnected ? 'Connected — Gmail monitoring is authorized.' : 'Not connected yet.'}</p>
            </div>
            <div className="flex items-center gap-3">
              <a href="/api/google/connect?capability=monitor" className="btn-primary rounded-lg px-4 py-2.5 text-sm font-medium transition-transform duration-150 focus-visible:outline-none focus-visible:ring-2">
                {me.googleConnected ? 'Reconnect Gmail' : 'Connect Gmail'}
              </a>
              {me.googleConnected && (
                <button
                  onClick={disconnectGoogle}
                  className="rounded-lg px-4 py-2.5 text-sm font-medium border border-line text-sand transition-colors duration-150 hover:border-line-hover hover:text-cream focus-visible:outline-none focus-visible:ring-2"
                >
                  Disconnect
                </button>
              )}
            </div>
          </div>
          <MonitoringStatus monitoring={me.monitoring} />
          {deliverability && <DomainSafety report={deliverability} />}
          <p className="text-xs mt-4 text-mute">
            Monitoring requests Gmail read access only. Sending and Sheets stay disconnected until you authorize each capability below. Tokens are stored encrypted.
          </p>
          <p className="text-xs mt-2 text-mute">
            Heads up: Google will show a "This app hasn't been verified" screen during connection — that's expected while Sendkeep is in Google's testing
            program. Click "Advanced" → "Go to Sendkeep" to continue.
          </p>
          <div className="mt-5 rounded-xl border border-line bg-floating p-4 text-xs leading-relaxed text-sand">
            <p className="font-medium text-cream">What this connection authorizes</p>
            <p className="mt-1.5">The core workflow reads bounded Gmail Sent-thread history to find promised actions and follow-up state. Gmail or Gemini remains your reply surface.</p>
            <div className="mt-3 grid gap-2 sm:grid-cols-2">
              <a href="/api/google/connect?capability=send" className="rounded-lg border border-line px-3 py-2 text-center font-medium text-cream hover:border-line-hover">
                {me.googleCapabilities.send ? 'Reauthorize Gmail sending' : 'Enable Gmail sending'}
              </a>
              <a href="/api/google/connect?capability=sheets" className="rounded-lg border border-line px-3 py-2 text-center font-medium text-cream hover:border-line-hover">
                {me.googleCapabilities.sheets ? 'Reauthorize Sheets & Drive' : 'Connect optional Sheets & Drive'}
              </a>
            </div>
            <p className="mt-2">Gmail sending is requested only after you choose to enable it. Sheets and Drive metadata are a separate optional grant.</p>
            <p className="mt-2">Disconnecting erases the stored token and stops Sendkeep monitoring and sends. Mail already in Gmail is not deleted.</p>
          </div>
        </section>

        <form onSubmit={handleSave} className="rounded-2xl p-6 card-shadow mb-6 bg-elevated border border-line">
          <h2 className="font-display text-lg mb-5" style={{ letterSpacing: '-0.01em' }}>
            Outreach settings
          </h2>
          {me.sendBlockers.length > 0 && (
            <div
              className="rounded-lg px-4 py-3 text-sm mb-5 border border-line"
              style={{ background: 'rgba(194,73,29,0.10)', color: 'var(--color-accent)' }}
            >
              <ul className="space-y-1">
                {me.sendBlockers.map((b) => (
                  <li key={b}>{b}</li>
                ))}
              </ul>
            </div>
          )}
          <div className="grid gap-4">
            <div className="text-sm">
              <span className="block mb-1.5 text-sand">Optional first-touch sheet</span>
              <div className="flex items-center justify-between gap-3 rounded-lg px-4 py-2.5 bg-floating border border-line">
                <span className="truncate text-mute">{sheetLabel}</span>
                <button
                  type="button"
                  onClick={toggleSheetPicker}
                  disabled={!me.googleCapabilities.sheets}
                  title={me.googleCapabilities.sheets ? '' : 'Authorize optional Sheets & Drive first'}
                  className="shrink-0 rounded-md px-3 py-1.5 text-xs font-medium border border-line text-sand transition-colors duration-150 hover:border-line-hover hover:text-cream focus-visible:outline-none focus-visible:ring-2 disabled:opacity-50 disabled:cursor-not-allowed"
                >
                  Choose sheet
                </button>
              </div>
              {sheetPickerOpen && (
                <div className="rounded-lg p-3 mt-2 bg-floating border border-line">
                  <div className="text-xs mb-2 text-mute">{sheetPickerStatus}</div>
                  <div className="max-h-56 overflow-y-auto space-y-1">
                    {sheets.map((s) => (
                      <button
                        key={s.id}
                        type="button"
                        onClick={() => pickSheet(s)}
                        className="w-full text-left rounded-md px-3 py-2 text-sm transition-colors duration-150 hover:bg-white/5 focus-visible:outline-none focus-visible:ring-2"
                      >
                        {s.name}
                      </button>
                    ))}
                  </div>
                </div>
              )}
              <button
                type="button"
                onClick={() => setManualSheetVisible((v) => !v)}
                className="text-xs mt-2 text-accent underline underline-offset-[3px] decoration-accent/40 rounded-[3px] transition-colors duration-200 ease-spring hover:text-accent-hover hover:decoration-accent-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/40 active:opacity-85"
              >
                Or paste a Sheet ID/URL manually
              </button>
              {manualSheetVisible && (
                <input
                  type="text"
                  value={form.google_sheet_id ?? ''}
                  onChange={(e) => setForm((f) => ({ ...f, google_sheet_id: e.target.value }))}
                  placeholder="Paste a Sheet ID or the full Sheets URL"
                  className="w-full rounded-lg px-4 py-2.5 text-sm mt-2 bg-floating border border-line focus:outline-none focus:border-accent"
                />
              )}
              <p className="text-xs mt-3 leading-relaxed text-mute">
                Starting from scratch? Download the starter sheet as{' '}
                <a
                  href="/api/lead-sheet-template.xlsx"
                  download
                  className="text-accent underline underline-offset-[3px] decoration-accent/40 rounded-[3px] transition-colors duration-200 ease-spring hover:text-accent-hover hover:decoration-accent-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/40 active:opacity-85"
                >
                  Excel
                </a>{' '}
                or{' '}
                <a
                  href="/api/lead-sheet-template.csv"
                  download
                  className="text-accent underline underline-offset-[3px] decoration-accent/40 rounded-[3px] transition-colors duration-200 ease-spring hover:text-accent-hover hover:decoration-accent-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/40 active:opacity-85"
                >
                  CSV
                </a>
                . Both carry the nine headers Sendkeep needs and an example row per status. Import
                one into Google Sheets, then delete the examples.
              </p>
            </div>

            <label className="text-sm">
              <span className="block mb-1.5 text-sand">
                Sender first name (signs your emails) <span className="text-sand/50">(required)</span>
              </span>
              <input
                type="text"
                value={form.sender_name ?? ''}
                onChange={(e) => setForm((f) => ({ ...f, sender_name: e.target.value }))}
                placeholder="Alex"
                className="w-full rounded-lg px-4 py-2.5 text-sm bg-floating border border-line focus:outline-none focus:border-accent"
              />
            </label>

            <fieldset className="rounded-xl border border-line bg-floating p-4 text-sm">
              <legend className="px-1 text-sand">First-touch sending</legend>
              <label className="flex cursor-pointer items-start gap-3">
                <input
                  type="radio"
                  name="outreach_send_mode"
                  value="manual"
                  checked={(form.outreach_send_mode || 'manual') === 'manual'}
                  onChange={() => setForm((f) => ({ ...f, outreach_send_mode: 'manual' }))}
                  className="mt-1 accent-[var(--color-accent)]"
                />
                <span>
                  <span className="block font-medium text-cream">Manual review — required by default</span>
                  <span className="mt-1 block text-xs text-sand">This is optional first-touch preparation. You edit and approve each email; the core promise workflow does not need it.</span>
                </span>
              </label>
              {me.autoSendEnabled ? (
                <label className="mt-4 flex cursor-pointer items-start gap-3 border-t border-line pt-4">
                  <input
                    type="radio"
                    name="outreach_send_mode"
                    value="auto"
                    checked={form.outreach_send_mode === 'auto'}
                    onChange={() => setForm((f) => ({ ...f, outreach_send_mode: 'auto' }))}
                    className="mt-1 accent-[var(--color-accent)]"
                  />
                  <span>
                    <span className="block font-medium text-cream">Auto-send — deployment enabled</span>
                    <span className="mt-1 block text-xs text-sand">Only use this in a controlled inbox pilot. Daily caps, opt-outs, address verification, bounce pauses, and spacing still apply.</span>
                  </span>
                </label>
              ) : (
                <p className="mt-4 border-t border-line pt-4 text-xs text-sand">
                  Automatic first-touch sending is disabled in this deployment. Google-safe by design: capped on purpose, with you reviewing every message. Gmail Sent monitoring remains read-only.
                </p>
              )}
            </fieldset>

            <label className="text-sm">
              <span className="block mb-1.5 text-sand">Follow-up delay</span>
              <span className="block text-xs text-mute mb-2">
                If nobody replies, show one follow-up reminder after this many business days. Sendkeep never creates a sequence.
              </span>
              <div className="flex items-center gap-3">
                <input
                  type="number"
                  min="1"
                  max="14"
                  value={form.follow_up_delay_days ?? 3}
                  onChange={(e) => setForm((f) => ({ ...f, follow_up_delay_days: Number(e.target.value) }))}
                  className="w-24 rounded-lg px-4 py-2.5 text-sm bg-floating border border-line focus:outline-none focus:border-accent"
                />
                <span className="text-xs text-sand">business days, one follow-up maximum</span>
              </div>
            </label>

            <label className="text-sm">
              <span className="block mb-1.5 text-sand">
                Company name (shown under your name in the signature) <span className="text-sand/50">(optional)</span>
              </span>
              <input
                type="text"
                value={form.sender_company ?? ''}
                onChange={(e) => setForm((f) => ({ ...f, sender_company: e.target.value }))}
                placeholder="Ledgerline"
                className="w-full rounded-lg px-4 py-2.5 text-sm bg-floating border border-line focus:outline-none focus:border-accent"
              />
            </label>

            <label className="text-sm">
              <span className="block mb-1.5 text-sand">
                What you're reaching out about (one sentence, used in every email) <span className="text-sand/50">(required)</span>
              </span>
              <input
                type="text"
                value={form.meeting_purpose ?? ''}
                onChange={(e) => setForm((f) => ({ ...f, meeting_purpose: e.target.value }))}
                placeholder="show finance teams how to cut invoice matching from days to minutes"
                className="w-full rounded-lg px-4 py-2.5 text-sm bg-floating border border-line focus:outline-none focus:border-accent"
              />
              <span className="block mt-1.5 text-xs text-sand/60">
                Say what you offer, who it helps, and what you want to happen (a call, a demo, a
                trial, a reply). The generic default is rejected at send time because it produces
                empty-sounding emails.
              </span>
            </label>

            <label className="text-sm">
              <span className="block mb-1.5 text-sand">Custom instructions <span className="text-sand/50">(optional)</span></span>
              <textarea
                rows={3}
                value={form.custom_instructions ?? ''}
                onChange={(e) => setForm((f) => ({ ...f, custom_instructions: e.target.value }))}
                placeholder="How you want your emails written. E.g. Casual, founder-to-founder tone. Mention we're YC-backed. Keep it under 4 sentences. Never use the word 'solution'."
                className="w-full rounded-lg px-4 py-2.5 text-sm bg-floating border border-line focus:outline-none focus:border-accent resize-y"
              />
              <span className="block mt-1.5 text-xs text-sand/60">
                Steers tone, content, length, and language on every email. The safety rules
                (plain text, honesty, the unsubscribe link, no placeholders) always win, so this
                can't break an email — it only shapes it.
              </span>
            </label>

            <label className="text-sm">
              <span className="block mb-1.5 text-sand">Call-to-action link <span className="text-sand/50">(optional)</span></span>
              <input
                type="url"
                value={form.calendar_booking_link ?? ''}
                onChange={(e) => setForm((f) => ({ ...f, calendar_booking_link: e.target.value }))}
                placeholder="https://cal.com/you/15min  ·  or a signup / demo / doc link"
                className="w-full rounded-lg px-4 py-2.5 text-sm bg-floating border border-line focus:outline-none focus:border-accent"
              />
              <span className="block mt-1.5 text-xs text-sand/60">
                Where you want people to go: a booking page, a signup, a demo, a resource.
                Leave it blank and each email simply asks for a reply.
              </span>
            </label>

          </div>

          <div className="flex items-center gap-3 mt-6">
            <button type="submit" className="btn-primary rounded-lg px-5 py-2.5 text-sm font-medium transition-transform duration-150 focus-visible:outline-none focus-visible:ring-2">
              Save settings
            </button>
            <span className="text-xs text-mute">{saveStatus}</span>
          </div>
        </form>

        <section className="rounded-2xl p-6 card-shadow bg-elevated border border-line">
          <h2 className="font-display text-lg mb-1" style={{ letterSpacing: '-0.01em' }}>
            Action alerts
          </h2>
          <p className="text-sm text-sand max-w-2xl">
            During the pilot, unresolved-reply, due-promise, reply or follow-up send-result, and Gmail connection alerts go only to <span className="text-cream font-medium">{me?.email}</span>. First-touch draft alerts are not included. Email content, contact names, addresses, promise text, and raw errors are never included.
          </p>
          <div className="flex flex-wrap items-center gap-3 mt-4">
            <button
              type="button"
              onClick={sendTestAlert}
              disabled={testAlertBusy}
              className="btn-ghost rounded-lg px-4 py-2.5 text-sm font-medium disabled:opacity-60"
            >
              {testAlertBusy ? 'Sending test…' : 'Send test alert'}
            </button>
            <span className="text-xs text-mute" role="status">{testAlertStatus}</span>
          </div>
        </section>

        <section className="rounded-2xl p-6 card-shadow bg-elevated border border-line">
          <h2 className="font-display text-lg mb-1" style={{ letterSpacing: '-0.01em' }}>
            Usage
          </h2>
          <p className="text-xs mb-5 text-mute">Metered against this account across all time.</p>
          <div className="grid grid-cols-2 gap-4 max-w-lg">
            <StatTile label="LLM tokens" value={usage.llm?.quantity || 0} />
            <StatTile label="Web searches" value={usage.search?.quantity || 0} />
          </div>
        </section>

        {plan && (
          <section className="rounded-2xl p-6 card-shadow mt-6 bg-elevated border border-line">
            <h2 className="font-display text-lg mb-1" style={{ letterSpacing: '-0.01em' }}>
              {plan.name} plan
            </h2>
            <p className="text-sm text-sand">
              ${plan.price_monthly_usd}/month per inbox · Gmail promise and follow-up memory. Safety cap: up to {plan.daily_send_limit} optional first-touch emails every 24 hours; the core reads Sent and never sends.
            </p>
            {(plan.monthly_drafts != null || plan.lead_allowance != null) && (
              <div className="flex flex-wrap gap-4 mt-4">
                {plan.monthly_drafts != null && (
                  <span className="text-xs text-sand">
                    <span className="text-cream font-medium">{plan.usage.drafts_used.toLocaleString('en-US')}</span> of{' '}
                    {plan.monthly_drafts.toLocaleString('en-US')} drafts used this month
                  </span>
                )}
                {plan.lead_allowance != null && (
                  <span className="text-xs text-sand">
                    <span className="text-cream font-medium">{plan.usage.leads_used.toLocaleString('en-US')}</span> of{' '}
                    {plan.lead_allowance.toLocaleString('en-US')} leads sourced
                  </span>
                )}
              </div>
            )}
            {plan.plan !== 'team' && (
              <div className="mt-5 pt-5 border-t border-line">
                <p className="text-sm text-cream font-medium">Upgrade this managed inbox</p>
                <p className="text-xs mt-1.5 text-sand">
                  Billing follows the product unit: one Sendkeep account is one isolated Gmail
                  inbox with its own promise log and audit trail.
                </p>
                {plan.checkout_available && plan.plan === 'trial' ? (
                  <div className="flex flex-wrap gap-3 mt-4">
                    <button
                      type="button"
                      onClick={() => startCheckout('pilot')}
                      disabled={billingBusy}
                      className="rounded-lg px-4 py-2.5 text-sm font-medium border border-line text-sand transition-colors duration-150 hover:border-line-hover hover:text-cream disabled:opacity-50 disabled:cursor-not-allowed focus-visible:outline-none focus-visible:ring-2"
                    >
                      {billingBusy ? 'Opening checkout…' : 'Inbox · $29/mo'}
                    </button>
                  </div>
                ) : (
                  <p className="text-xs mt-3 text-mute">
                    {plan.plan === 'pilot'
                      ? 'Inbox is $29/mo. Agency pilot is $49/mo per managed Gmail inbox with concierge onboarding.'
                      : 'Online checkout is not configured on this deployment.'}
                    {plan.plan === 'pilot' && PUBLIC_BOOKING_URL && (
                      <>{' '}<a className="text-accent underline underline-offset-2" href={PUBLIC_BOOKING_URL} target="_blank" rel="noreferrer">Book the agency interview</a>.</>
                    )}
                  </p>
                )}
              </div>
            )}
          </section>
        )}

        <section className="rounded-2xl p-6 card-shadow mt-6 bg-elevated border border-line">
          <h2 className="font-display text-lg mb-1" style={{ letterSpacing: '-0.01em' }}>
            Your data & exit
          </h2>
          <p className="text-xs leading-relaxed text-sand">
            Download a portable JSON copy of your Sendkeep tracked threads, commitments, follow-up outcomes, audit events, and usage. OAuth credentials and session secrets are never included. Gmail messages and Google Sheet rows remain in Google.
          </p>
          <div className="mt-4 flex flex-wrap items-center gap-3">
            <button
              type="button"
              onClick={exportAccountData}
              disabled={exportBusy}
              className="rounded-lg px-4 py-2.5 text-sm font-medium border border-line text-sand transition-colors duration-150 hover:border-line-hover hover:text-cream disabled:opacity-50 disabled:cursor-not-allowed focus-visible:outline-none focus-visible:ring-2"
            >
              {exportBusy ? 'Preparing export…' : 'Download my data'}
            </button>
            <a className="text-xs text-accent underline underline-offset-2" href="mailto:support@sendkeep.app?subject=Delete%20my%20Sendkeep%20data">
              Request account deletion
            </a>
          </div>
        </section>
      </main>
    </div>
  )
}
