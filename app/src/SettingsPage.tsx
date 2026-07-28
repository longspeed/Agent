import { useEffect, useState } from 'react'
import { NavBar } from './components/NavBar'
import { useAnimatedCount } from './hooks/useAnimatedCount'

interface AccountSettings {
  sender_name: string | null
  sender_company: string | null
  meeting_purpose: string | null
  custom_instructions: string | null
  calendar_booking_link: string | null
  notify_email: string | null
  google_sheet_id: string | null
}

interface MeResponse {
  email: string
  googleConnected: boolean
  settings: AccountSettings
  onboarded: boolean
}

interface UsageSummary {
  [kind: string]: { events: number; quantity: number }
}

interface PlanInfo {
  price_monthly_usd: number
  daily_send_limit: number
  lead_searches_per_hour: number
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
}

function CheckIcon() {
  return (
    <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="#0F0E0C" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round">
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
        {animated.toLocaleString()}
      </div>
      <div className="text-xs mt-1 text-sand">{label}</div>
    </div>
  )
}

export function SettingsPage() {
  const [me, setMe] = useState<MeResponse | null>(null)
  const [usage, setUsage] = useState<UsageSummary>({})
  const [plan, setPlan] = useState<PlanInfo | null>(null)
  const [form, setForm] = useState<AccountSettings>(EMPTY_FORM)
  const [saveStatus, setSaveStatus] = useState('')
  const [flash, setFlash] = useState<{ text: string; ok: boolean } | null>(null)

  const [sheetPickerOpen, setSheetPickerOpen] = useState(false)
  const [sheets, setSheets] = useState<SheetOption[]>([])
  const [sheetPickerStatus, setSheetPickerStatus] = useState('')
  const [manualSheetVisible, setManualSheetVisible] = useState(false)

  async function load() {
    const meRes: MeResponse = await (await fetch('/api/me')).json()
    setMe(meRes)
    setForm({
      sender_name: meRes.settings.sender_name || '',
      sender_company: meRes.settings.sender_company || '',
      meeting_purpose: meRes.settings.meeting_purpose || '',
      custom_instructions: meRes.settings.custom_instructions || '',
      calendar_booking_link: meRes.settings.calendar_booking_link || '',
      notify_email: meRes.settings.notify_email || '',
      google_sheet_id: meRes.settings.google_sheet_id || '',
    })

    const usageRes = await (await fetch('/api/usage')).json()
    setUsage(usageRes)

    const planRes = await (await fetch('/api/plan')).json()
    setPlan(planRes)
  }

  useEffect(() => {
    load()

    const params = new URLSearchParams(window.location.search)
    if (params.get('connected')) setFlash({ text: 'Google account connected.', ok: true })
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
    }
    const res = await fetch('/api/settings', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
    setSaveStatus(res.ok ? 'Saved.' : 'Save failed — try again.')
    setTimeout(() => setSaveStatus(''), 2500)
    if (res.ok) load()
  }

  async function loadSheetsList() {
    if (!me?.googleConnected) {
      setSheetPickerStatus('Connect Google first (above) to browse your Sheets.')
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

  if (!me) {
    return (
      <div className="min-h-screen antialiased">
        <NavBar breadcrumb="Settings" />
        <main className="relative max-w-3xl mx-auto px-6 py-10 text-sm text-sand">Loading…</main>
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
            'radial-gradient(ellipse 800px 500px at 15% -10%, rgba(232,98,44,0.10), transparent), radial-gradient(ellipse 600px 400px at 90% 10%, rgba(76,134,168,0.08), transparent)',
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
            style={{ background: flash.ok ? 'rgba(123,182,98,0.12)' : 'rgba(232,98,44,0.12)', color: flash.ok ? 'var(--color-ok)' : 'var(--color-accent)' }}
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
                Connect your Google account (Gmail + Sheets)
              </li>
              <li className="flex items-center gap-2.5" style={{ color: sheetOk ? 'var(--color-mute)' : undefined, textDecoration: sheetOk ? 'line-through' : 'none' }}>
                <StepDot done={sheetOk} />
                Pick the Google Sheet that holds your leads
              </li>
              <li className="flex items-center gap-2.5" style={{ color: calOk ? 'var(--color-mute)' : undefined, textDecoration: calOk ? 'line-through' : 'none' }}>
                <StepDot done={calOk} />
                Add your calendar booking link
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
                Google account
              </h2>
              <p className="text-sm text-sand">{me.googleConnected ? 'Connected — Gmail and Sheets are authorized.' : 'Not connected yet.'}</p>
            </div>
            <div className="flex items-center gap-3">
              <a href="/api/google/connect" className="btn-primary rounded-lg px-4 py-2.5 text-sm font-medium transition-transform duration-150 focus-visible:outline-none focus-visible:ring-2">
                {me.googleConnected ? 'Reconnect' : 'Connect Google'}
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
          <p className="text-xs mt-4 text-mute">
            Outreach emails are sent from this Gmail; your lead sheet is read and updated with its permission. The token is stored encrypted.
          </p>
          <p className="text-xs mt-2 text-mute">
            Heads up: Google will show a "This app hasn't been verified" screen during connection — that's expected while Agent Hub is in Google's testing
            program. Click "Advanced" → "Go to Agent Hub" to continue.
          </p>
        </section>

        <form onSubmit={handleSave} className="rounded-2xl p-6 card-shadow mb-6 bg-elevated border border-line">
          <h2 className="font-display text-lg mb-5" style={{ letterSpacing: '-0.01em' }}>
            Outreach settings
          </h2>
          <div className="grid gap-4">
            <div className="text-sm">
              <span className="block mb-1.5 text-sand">Lead sheet</span>
              <div className="flex items-center justify-between gap-3 rounded-lg px-4 py-2.5 bg-floating border border-line">
                <span className="truncate text-mute">{sheetLabel}</span>
                <button
                  type="button"
                  onClick={toggleSheetPicker}
                  disabled={!me.googleConnected}
                  title={me.googleConnected ? '' : 'Connect Google first'}
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
              <button type="button" onClick={() => setManualSheetVisible((v) => !v)} className="text-xs mt-2 text-mute transition-colors duration-150">
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
                . Both carry the nine headers Agent Hub needs and an example row per status. Import
                one into Google Sheets, then delete the examples.
              </p>
            </div>

            <label className="text-sm">
              <span className="block mb-1.5 text-sand">Sender first name (signs your emails)</span>
              <input
                type="text"
                value={form.sender_name ?? ''}
                onChange={(e) => setForm((f) => ({ ...f, sender_name: e.target.value }))}
                placeholder="Alex"
                className="w-full rounded-lg px-4 py-2.5 text-sm bg-floating border border-line focus:outline-none focus:border-accent"
              />
            </label>

            <label className="text-sm">
              <span className="block mb-1.5 text-sand">Company name (shown under your name in the signature)</span>
              <input
                type="text"
                value={form.sender_company ?? ''}
                onChange={(e) => setForm((f) => ({ ...f, sender_company: e.target.value }))}
                placeholder="Ledgerline"
                className="w-full rounded-lg px-4 py-2.5 text-sm bg-floating border border-line focus:outline-none focus:border-accent"
              />
            </label>

            <label className="text-sm">
              <span className="block mb-1.5 text-sand">What you're reaching out about (one sentence, used in every email)</span>
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

            <label className="text-sm">
              <span className="block mb-1.5 text-sand">Notification email (batch summaries & reply alerts)</span>
              <input
                type="email"
                value={form.notify_email ?? ''}
                onChange={(e) => setForm((f) => ({ ...f, notify_email: e.target.value }))}
                placeholder="you@company.com"
                className="w-full rounded-lg px-4 py-2.5 text-sm bg-floating border border-line focus:outline-none focus:border-accent"
              />
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
              Pilot plan
            </h2>
            <p className="text-sm text-sand">
              ${plan.price_monthly_usd}/month · up to {plan.daily_send_limit} approved emails per inbox every 24 hours · {plan.lead_searches_per_hour}{' '}
              lead searches per hour.
            </p>
            <p className="text-xs mt-3 text-mute">This is a pricing placeholder while checkout is being validated with early customers.</p>
          </section>
        )}
      </main>
    </div>
  )
}
