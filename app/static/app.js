'use strict';

const $ = selector => document.querySelector(selector);
const escape = value => String(value ?? '').replace(/[&<>"']/g, character => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[character]));
const statuses = new Set(['pending', 'processing', 'confirmed', 'rejected']);
const terminal = booking => booking && ['confirmed', 'rejected'].includes(booking.status);
const formatNumber = value => Number.isFinite(value) ? new Intl.NumberFormat('en-IN').format(value) : '—';
const formatPrice = value => Number.isFinite(value) ? new Intl.NumberFormat('en-IN', {style: 'currency', currency: 'INR', maximumFractionDigits: 0}).format(value / 100) : '—';
const shortId = id => String(id ?? '').slice(0, 8).toUpperCase();
const state = {dashboard: null, selectedEventId: '', submitting: false, refreshing: false, refreshAgain: false, timer: null, ownBookings: new Map(), trackedIds: new Set(), pendingIntent: null, connection: false, includeDemo: false};

async function fetchWithTimeout(url, options = {}, timeout = 10000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout);
  try { return await fetch(url, {...options, signal: controller.signal}); }
  catch (error) { if (error.name === 'AbortError') throw Error('The service took too long to respond.'); throw error; }
  finally { clearTimeout(timer); }
}

function readSession(key, fallback) {
  try { return JSON.parse(sessionStorage.getItem(key)) ?? fallback; } catch { return fallback; }
}

function writeSession(key, value) {
  try { sessionStorage.setItem(key, JSON.stringify(value)); } catch { /* The page still works if browser storage is unavailable. */ }
}

const savedIntent = readSession('ticketflow.pending-intent', null);
if (savedIntent && typeof savedIntent.key === 'string' && typeof savedIntent.payload?.event_id === 'string' && typeof savedIntent.payload?.customer_name === 'string' && Number.isInteger(savedIntent.payload?.quantity) && savedIntent.payload.quantity >= 1 && savedIntent.payload.quantity <= 6) {
  state.pendingIntent = savedIntent;
  state.selectedEventId = savedIntent.payload.event_id;
  $('#guest-alias').value = savedIntent.payload.customer_name;
  $('#ticket-quantity').value = savedIntent.payload.quantity;
}
const savedIds = readSession('ticketflow.tracked-bookings', []);
if (Array.isArray(savedIds)) savedIds.filter(id => typeof id === 'string').slice(-10).forEach(id => state.trackedIds.add(id));

function rememberBooking(booking) {
  state.ownBookings.set(booking.id, booking);
  state.trackedIds.add(booking.id);
  if (state.trackedIds.size > 10) state.trackedIds.delete(state.trackedIds.values().next().value);
  writeSession('ticketflow.tracked-bookings', [...state.trackedIds]);
}

function updateOwnedBooking(booking) {
  const previous = state.ownBookings.get(booking.id);
  state.ownBookings.set(booking.id, booking);
  if (previous && previous.status !== booking.status && terminal(booking)) {
    $('#booking-feedback').textContent = booking.status === 'confirmed' ? `Booking #${shortId(booking.id)} confirmed. ${booking.quantity} ticket${booking.quantity === 1 ? '' : 's'} reserved for ${booking.event_title}.` : `Booking #${shortId(booking.id)} was not reserved. ${booking.rejection_reason || 'See the ledger for details.'}`;
  }
}

function showFormError(message) {
  $('#form-error').textContent = message;
  $('#form-error').hidden = !message;
}

function setConnection(online, message) {
  state.connection = online;
  const status = $('#connection-status');
  status.classList.toggle('is-live', online);
  status.classList.toggle('is-offline', !online);
  if (status.lastElementChild.textContent !== message) status.lastElementChild.textContent = message;
}

function eventList() {
  return (state.dashboard?.events ?? []).filter(event => !event.is_demo);
}

function renderEvents() {
  const events = eventList();
  const selected = state.pendingIntent?.payload.event_id || state.selectedEventId;
  if (!events.some(event => event.id === selected) && !state.pendingIntent) {
    state.selectedEventId = events.find(event => event.available_seats > 0)?.id || events[0]?.id || '';
  }
  const selection = state.pendingIntent?.payload.event_id || state.selectedEventId;
  const grid = $('#events-grid');
  const focusedEventId = document.activeElement?.dataset?.eventId;
  grid.setAttribute('aria-busy', 'false');
  $('#event-count').textContent = `${events.length} event${events.length === 1 ? '' : 's'} to explore`;
  grid.innerHTML = events.length ? events.map((event, index) => {
    const available = Number.isFinite(event.available_seats) ? event.available_seats : null;
    const capacity = Number.isFinite(event.total_seats) ? event.total_seats : null;
    const percent = capacity > 0 && available !== null ? Math.max(0, Math.min(100, available / capacity * 100)) : 0;
    const active = event.id === selection;
    const theme = {teal: 0, coral: 1, violet: 2}[event.accent] ?? index % 3;
    const artWord = String(event.title || 'An experience').split(' ').slice(0, 2).join(' ');
    const disabled = available === 0 || state.submitting || Boolean(state.pendingIntent);
    return `<article class="event-card ${active ? 'is-selected' : ''}"><div class="event-art theme-${theme}" aria-hidden="true"><span class="art-category">${escape(event.category || 'EXPERIENCE')}</span><div class="art-word">${escape(artWord)}</div><span class="art-corner">↗</span></div><div class="event-info"><div class="event-meta"><span>${escape(event.date_label)}</span><span>${available === 0 ? 'SOLD OUT' : 'LIVE INVENTORY'}</span></div><h3>${escape(event.title)}</h3><div class="event-venue">${escape(event.venue)}</div><div class="event-availability"><strong>${available === null ? 'Availability unknown' : `${formatNumber(available)} seats left`}</strong><span>${capacity === null ? '' : `of ${formatNumber(capacity)}`}</span></div><div class="availability-track" aria-hidden="true"><i style="width:${percent}%"></i></div><div class="event-bottom"><div class="event-price">${formatPrice(event.price_cents)}<small>demo value / ticket</small></div><button class="event-select ${active ? 'is-selected' : ''}" type="button" data-event-id="${escape(event.id)}" aria-pressed="${active}" aria-label="${escape(`${active ? 'Selected: ' : 'Select '}${event.title}`)}" ${disabled ? 'disabled' : ''}>${available === 0 ? 'Sold out' : active ? 'Selected' : 'Choose event'} <span aria-hidden="true">${active ? '✓' : '↗'}</span></button></div></div></article>`;
  }).join('') : '<p class="empty-note">There are no events to book yet. Check back when the event inventory is ready.</p>';
  if (focusedEventId) [...grid.querySelectorAll('[data-event-id]')].find(button => button.dataset.eventId === focusedEventId)?.focus({preventScroll: true});

  const select = $('#event-select');
  const previous = select.value;
  const options = events.map(event => `<option value="${escape(event.id)}" ${event.available_seats === 0 ? 'disabled' : ''}>${escape(event.title)}${event.available_seats === 0 ? ' · sold out' : ''}</option>`).join('');
  // Keep a request restored from this browser even if its event is no longer in the showcase.
  const extra = state.pendingIntent && !events.some(event => event.id === selection) ? `<option value="${escape(selection)}">Previous request · retry to check</option>` : '';
  const nextOptions = options + extra || '<option value="">No events available</option>';
  if (select.innerHTML !== nextOptions) select.innerHTML = nextOptions;
  select.value = selection || previous;
  renderSelectedEvent();
  updateFormState();
}

function renderSelectedEvent() {
  const event = (state.dashboard?.events ?? []).find(event => event.id === (state.pendingIntent?.payload.event_id || state.selectedEventId));
  $('#selected-event-note').textContent = state.pendingIntent ? 'Your previous request is saved. Retry safely to check its outcome.' : event ? `${formatNumber(event.available_seats)} seats available. Final availability is checked by a worker.` : 'Choose an available event to get started.';
}

function renderMetrics() {
  const metrics = state.dashboard.metrics ?? {};
  $('#metric-queued').textContent = formatNumber(metrics.pending);
  $('#metric-processing').textContent = formatNumber(metrics.processing);
  $('#metric-confirmed').textContent = formatNumber(metrics.confirmed);
  $('#metric-rejected').textContent = formatNumber(metrics.rejected);
  $('#queue-health').innerHTML = `<span>Outstanding jobs</span><strong>${Number.isFinite(metrics.queue_depth) ? `${formatNumber(metrics.queue_depth)} pending or processing` : 'Unavailable'}</strong>`;
}

function timeLabel(value) {
  const date = new Date(value);
  if (!value || Number.isNaN(date.getTime())) return '';
  return date.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'});
}

function relativeTime(value) {
  const milliseconds = new Date(value).getTime();
  if (!value || !Number.isFinite(milliseconds)) return '';
  const seconds = Math.max(0, Math.floor((Date.now() - milliseconds) / 1000));
  return seconds < 10 ? 'just now' : seconds < 60 ? `${seconds}s ago` : seconds < 3600 ? `${Math.floor(seconds / 60)}m ago` : `${Math.floor(seconds / 3600)}h ago`;
}

function allBookings() {
  const bookings = new Map((state.dashboard?.bookings ?? []).map(booking => [booking.id, booking]));
  state.ownBookings.forEach((booking, id) => bookings.set(id, booking));
  return [...bookings.values()].sort((a, b) => new Date(b.created_at) - new Date(a.created_at)).slice(0, 40);
}

function renderBookings() {
  const bookings = allBookings();
  $('#bookings-body').innerHTML = bookings.length ? bookings.map(booking => {
    const status = statuses.has(booking.status) ? booking.status : 'unknown';
    const statusLabel = status[0].toUpperCase() + status.slice(1);
    const detail = booking.rejection_reason || (booking.worker_id ? `Worker ${booking.worker_id}` : 'Waiting for a worker');
    return `<tr><td><span class="booking-id" title="${escape(booking.id)}">#${escape(shortId(booking.id))}</span><div class="booking-guest" title="${escape(booking.customer_name)}">${escape(booking.customer_name)}</div></td><td><div class="booking-event" title="${escape(booking.event_title)}">${escape(booking.event_title)}</div><time class="booking-time" datetime="${escape(booking.created_at)}">${escape(timeLabel(booking.created_at))}</time></td><td>${formatNumber(booking.quantity)}</td><td><span class="status-pill status-${status}" title="${escape(detail)}">${escape(statusLabel)}</span>${booking.rejection_reason ? `<small class="rejection-detail">${escape(booking.rejection_reason)}</small>` : ''}</td></tr>`;
  }).join('') : '<tr><td colspan="4" class="table-empty"><span class="empty-ledger-icon" aria-hidden="true">▤</span>No bookings yet. Your first request starts a new story.</td></tr>';
}

function renderWorkers() {
  const workers = state.dashboard.workers ?? [];
  const online = workers.filter(worker => worker.online);
  const offline = workers.filter(worker => !worker.online);
  const historyOpen = $('#worker-history')?.open;
  const workerCard = worker => {
    const status = worker.online === false ? 'offline' : worker.status === 'busy' ? 'busy' : worker.status === 'idle' ? 'idle' : 'offline';
    const label = {busy: 'Processing', idle: 'Ready', offline: 'Offline'}[status];
    const task = status === 'busy' && worker.current_booking_id ? `Working on #${shortId(worker.current_booking_id)}` : status === 'offline' ? 'No recent heartbeat' : 'Listening for the next request';
    return `<article class="worker-card"><span class="worker-icon" aria-hidden="true">${status === 'busy' ? '↻' : '◫'}</span><div class="worker-details"><div class="worker-name">${escape(worker.id)}</div><div class="worker-task">${escape(task)}${Number.isFinite(worker.processed_count) ? ` · ${formatNumber(worker.processed_count)} processed, all runs` : ''}</div></div><span class="worker-state ${status}">${label}</span></article>`;
  };
  $('#workers-list').innerHTML = (online.length ? online.map(workerCard).join('') : '<p class="empty-note">No workers are online. Requests remain queued until a worker is available.</p>') + (offline.length ? `<details class="worker-history" id="worker-history" ${historyOpen ? 'open' : ''}><summary>${offline.length} offline worker${offline.length === 1 ? '' : 's'}</summary><div class="workers-history-list">${offline.map(workerCard).join('')}</div></details>` : '');
}

function renderActivity() {
  const activity = state.dashboard.activity ?? [];
  $('#activity-list').innerHTML = activity.length ? activity.slice(0, 6).map(item => `<li class="activity-item">${escape(item.message)}<small>${escape(item.worker_id || 'Booking service')}${item.created_at ? ` · ${escape(relativeTime(item.created_at))}` : ''}</small></li>`).join('') : '<li class="empty-note">No activity yet. Queue a request to follow it through the system.</li>';
}

function updateFormState() {
  const locked = state.submitting || Boolean(state.pendingIntent);
  const event = eventList().find(item => item.id === state.selectedEventId);
  $('#event-select').disabled = locked || eventList().length === 0;
  $('#guest-alias').disabled = locked;
  $('#ticket-quantity').disabled = locked;
  $('#quantity-decrease').disabled = locked || Number($('#ticket-quantity').value) <= 1;
  $('#quantity-increase').disabled = locked || Number($('#ticket-quantity').value) >= 6;
  $('#booking-submit').disabled = state.submitting || (!state.pendingIntent && (!state.connection || !event || event.available_seats === 0));
  $('#booking-submit').firstElementChild.textContent = state.submitting ? 'Sending request…' : state.pendingIntent ? 'Retry the same request' : 'Request tickets';
  document.querySelectorAll('[data-event-id]').forEach(button => {
    const item = eventList().find(event => event.id === button.dataset.eventId);
    button.disabled = locked || item?.available_seats === 0;
  });
}

function requestError(data, fallback) {
  if (typeof data?.detail === 'string') return data.detail;
  if (Array.isArray(data?.detail)) return data.detail.map(item => `${(item.loc ?? []).filter(part => part !== 'body').join(' ')}: ${item.msg || 'Invalid value'}`).join('. ');
  return fallback;
}

async function submitIntent() {
  if (state.submitting || !state.pendingIntent) return;
  state.submitting = true;
  showFormError('');
  updateFormState();
  try {
    const response = await fetchWithTimeout('/api/bookings', {method: 'POST', headers: {'Content-Type': 'application/json', 'Idempotency-Key': state.pendingIntent.key}, body: JSON.stringify(state.pendingIntent.payload)}, 20000);
    const data = await response.json().catch(() => null);
    if (response.status >= 500) throw Error(requestError(data, 'The booking service could not confirm the outcome.'));
    if (!response.ok) {
      state.pendingIntent = null;
      writeSession('ticketflow.pending-intent', null);
      showFormError(requestError(data, `Your request was not accepted (HTTP ${response.status}).`));
      return;
    }
    // An unreadable success may have committed. Keep the same key until we know its booking ID.
    if (!data || typeof data.id !== 'string' || !statuses.has(data.status)) throw Error('The booking service returned an incomplete response.');
    state.pendingIntent = null;
    writeSession('ticketflow.pending-intent', null);
    rememberBooking(data);
    $('#booking-feedback').textContent = `${data.replayed ? 'Recovered your previous request' : 'Request received'} · #${shortId(data.id)} for ${data.quantity} ticket${data.quantity === 1 ? '' : 's'}. ${data.status === 'confirmed' ? 'Your reservation is confirmed.' : data.status === 'rejected' ? 'The reservation was declined; see the ledger for details.' : 'Follow its live status in the ledger below.'}`;
    renderBookings();
    void refreshDashboard();
  } catch (error) {
    showFormError(`${error.message} Your request is saved. Retry the same request to check its outcome without creating a duplicate.`);
  } finally {
    state.submitting = false;
    renderSelectedEvent();
    updateFormState();
  }
}

$('#booking-form').addEventListener('submit', event => {
  event.preventDefault();
  if (state.submitting) return;
  if (!state.pendingIntent) {
    if (!$('#booking-form').reportValidity()) return;
    const name = $('#guest-alias').value.trim();
    if (!name) { showFormError('Please enter a name or alias.'); $('#guest-alias').focus(); return; }
    state.pendingIntent = {key: crypto.randomUUID(), payload: {event_id: state.selectedEventId, customer_name: name, quantity: Number($('#ticket-quantity').value)}};
    writeSession('ticketflow.pending-intent', state.pendingIntent);
  }
  void submitIntent();
});

$('#event-select').addEventListener('change', event => { state.selectedEventId = event.target.value; showFormError(''); renderEvents(); });
$('#events-grid').addEventListener('click', event => {
  const button = event.target.closest('[data-event-id]');
  if (!button || state.pendingIntent || state.submitting) return;
  state.selectedEventId = button.dataset.eventId;
  showFormError('');
  renderEvents();
  $('#guest-alias').focus({preventScroll: true});
  if (matchMedia('(max-width: 630px)').matches) $('.booking-panel').scrollIntoView({behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth', block: 'start'});
});

function changeQuantity(amount) {
  const input = $('#ticket-quantity');
  input.value = Math.max(1, Math.min(6, (Number(input.value) || 1) + amount));
  updateFormState();
}
$('#quantity-decrease').addEventListener('click', () => changeQuantity(-1));
$('#quantity-increase').addEventListener('click', () => changeQuantity(1));
$('#ticket-quantity').addEventListener('input', updateFormState);

async function refreshOwnBookings() {
  const recent = new Map((state.dashboard.bookings ?? []).map(booking => [booking.id, booking]));
  await Promise.all([...state.trackedIds].map(async id => {
    if (recent.has(id)) { updateOwnedBooking(recent.get(id)); return; }
    if (terminal(state.ownBookings.get(id))) return;
    try {
      const response = await fetchWithTimeout(`/api/bookings/${encodeURIComponent(id)}`);
      if (response.status === 404) { state.trackedIds.delete(id); state.ownBookings.delete(id); writeSession('ticketflow.tracked-bookings', [...state.trackedIds]); return; }
      if (!response.ok) return;
      const booking = await response.json();
      if (booking.id === id) updateOwnedBooking(booking);
    } catch { /* Preserve its last known status until the next dashboard refresh. */ }
  }));
}

function scheduleRefresh() {
  clearTimeout(state.timer);
  const outstanding = allBookings().some(booking => !terminal(booking));
  state.timer = setTimeout(refreshDashboard, outstanding || state.submitting ? 1000 : 2000);
}

async function refreshDashboard() {
  if (state.refreshing) { state.refreshAgain = true; return; }
  state.refreshing = true;
  clearTimeout(state.timer);
  try {
    const requestedScope = state.includeDemo;
    const response = await fetchWithTimeout(`/api/dashboard${requestedScope ? '?include_demo=true' : ''}`, {cache: 'no-store'});
    if (!response.ok) throw Error(`Dashboard unavailable (HTTP ${response.status}).`);
    const data = await response.json();
    if (!Array.isArray(data.events) || !Array.isArray(data.bookings) || typeof data.metrics !== 'object' || data.metrics === null) throw Error('The dashboard response was incomplete.');
    // A scope change during an in-flight poll must not relabel the old scope's data.
    if (requestedScope !== state.includeDemo) return;
    state.dashboard = data;
    $('#data-scope-note').textContent = state.includeDemo ? 'Metrics, ledger and activity include demo runs. Featured events stay unchanged.' : 'Showing showcase bookings. Background demo runs are excluded.';
    await refreshOwnBookings();
    const workers = (data.workers ?? []).filter(worker => worker.online).length;
    setConnection(true, `API live · ${workers} worker${workers === 1 ? '' : 's'} online`);
    $('#connection-error').hidden = true;
    renderMetrics();
    renderEvents();
    renderBookings();
    renderWorkers();
    renderActivity();
  } catch (error) {
    setConnection(false, 'Connection interrupted');
    $('#connection-error').textContent = `${error.message} ${state.dashboard ? 'Showing the last received data; booking updates may be delayed.' : 'Start the API and its database/queue services to load live information.'} Reconnecting automatically.`;
    $('#connection-error').hidden = false;
    if (!state.dashboard) {
      $('#event-count').textContent = 'Waiting for live data';
      $('#events-grid').setAttribute('aria-busy', 'false');
      $('#events-grid').innerHTML = '<p class="empty-note">Event inventory will appear when the connection is restored.</p>';
      $('#bookings-body').innerHTML = '<tr><td colspan="4" class="table-empty">Waiting for the booking service. No sample bookings are shown.</td></tr>';
    }
    updateFormState();
  } finally {
    state.refreshing = false;
    if (state.refreshAgain) { state.refreshAgain = false; state.timer = setTimeout(refreshDashboard, 0); }
    else scheduleRefresh();
  }
}

if (state.pendingIntent) showFormError('A previous request has an unknown outcome. Retry the same request to recover its status safely.');
updateFormState();
void refreshDashboard();
document.addEventListener('visibilitychange', () => { if (!document.hidden) void refreshDashboard(); });
$('#include-demo').addEventListener('change', event => {
  state.includeDemo = event.target.checked;
  $('#data-scope-note').textContent = 'Updating the dashboard scope…';
  void refreshDashboard();
});
