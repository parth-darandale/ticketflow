'use strict';

const $ = selector => document.querySelector(selector);
const escape = value => String(value ?? '').replace(/[&<>"']/g, character => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[character]));
const statuses = new Set(['pending', 'processing', 'confirmed', 'rejected']);
const terminal = booking => booking && ['confirmed', 'rejected'].includes(booking.status);
const formatNumber = value => Number.isFinite(value) ? new Intl.NumberFormat('en-IN').format(value) : '—';
const formatPrice = value => Number.isFinite(value) ? new Intl.NumberFormat('en-IN', {style: 'currency', currency: 'INR', maximumFractionDigits: 0}).format(value / 100) : '—';
const shortId = id => String(id ?? '').slice(0, 8).toUpperCase();
const state = {dashboard: null, selectedEventId: '', submitting: false, refreshing: false, refreshAgain: false, timer: null, ownBookings: new Map(), trackedIds: new Set(), pendingIntent: null, connection: false, includeDemo: false, traceBookingId: '', traceData: null, traceError: '', traceLoading: false, traceRequest: 0, fetchingTraceId: '', control: null, controlRefreshing: false, controlTimer: null, shutdownRequesting: false, shutdownAccepted: false, shutdownMonitorTimer: null};

const shutdownLocked = () => state.shutdownRequesting || state.shutdownAccepted;

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

function renderShutdownControl() {
  const enabled = state.control?.enabled === true && state.control?.state === 'ready';
  const button = $('#shutdown-button');
  button.disabled = !enabled || state.submitting || shutdownLocked();
  const hint = state.shutdownAccepted ? 'TicketFlow is stopping. Run Start-App.cmd to start again.' : state.shutdownRequesting ? 'Sending the shutdown request.' : state.submitting ? 'Wait for your booking request to finish before stopping TicketFlow.' : enabled ? 'Stop TicketFlow services while preserving saved bookings.' : 'Run Start-App.cmd to enable the shutdown button.';
  button.title = hint;
  $('#shutdown-hint').textContent = hint;
}

async function refreshControl() {
  if (state.shutdownAccepted || state.controlRefreshing) return;
  state.controlRefreshing = true;
  clearTimeout(state.controlTimer);
  try {
    const response = await fetchWithTimeout('/api/system/control', {cache: 'no-store'}, 4000);
    if (!response.ok) throw Error('Shutdown control is unavailable.');
    const control = await response.json();
    if (!control || typeof control.enabled !== 'boolean' || typeof control.state !== 'string') throw Error('Shutdown information is incomplete.');
    if (state.shutdownAccepted) return;
    state.control = control;
    // Another open tab can initiate the same local shutdown.
    if (control.state === 'stopping') acceptShutdown();
  } catch {
    if (!state.shutdownAccepted) state.control = null;
  } finally {
    state.controlRefreshing = false;
    renderShutdownControl();
    if (!state.shutdownAccepted) state.controlTimer = setTimeout(refreshControl, 3000);
  }
}

function showShutdownNotice(message) {
  $('#shutdown-notice').hidden = false;
  $('#shutdown-notice').textContent = message;
}

function acceptShutdown() {
  if (state.shutdownAccepted) return;
  state.shutdownAccepted = true;
  state.shutdownRequesting = false;
  state.refreshAgain = false;
  state.traceRequest += 1;
  clearTimeout(state.timer);
  clearTimeout(state.controlTimer);
  $('#shutdown-dialog').close();
  $('#connection-error').hidden = true;
  setConnection(false, 'Stopping TicketFlow');
  showShutdownNotice('Stopping TicketFlow… Your saved bookings are preserved. Run Start-App.cmd with Docker Desktop running to start again.');
  updateFormState();
  $('#shutdown-notice').focus({preventScroll: true});
  void monitorShutdown(Date.now() + 20000);
}

async function monitorShutdown(deadline) {
  try {
    await fetchWithTimeout('/api/health', {cache: 'no-store'}, 1500);
  } catch {
    setConnection(false, 'TicketFlow stopped');
    showShutdownNotice('TicketFlow stopped. Your saved bookings are preserved. Run Start-App.cmd with Docker Desktop running to start again, then reload this page.');
    return;
  }
  if (Date.now() >= deadline) {
    setConnection(false, 'Shutdown requested');
    showShutdownNotice('Shutdown was requested, but TicketFlow is still responding. Run Stop-App.cmd if it stays online. Start-App.cmd starts it again.');
    return;
  }
  state.shutdownMonitorTimer = setTimeout(() => void monitorShutdown(deadline), 1000);
}

async function requestShutdown() {
  if (shutdownLocked() || state.submitting) return;
  state.shutdownRequesting = true;
  $('#shutdown-dialog-error').hidden = true;
  $('#shutdown-confirm').disabled = true;
  $('#shutdown-confirm').textContent = 'Stopping…';
  $('#shutdown-cancel').disabled = true;
  updateFormState();
  try {
    const response = await fetchWithTimeout('/api/system/shutdown', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-TicketFlow-Control': 'shutdown'}, body: JSON.stringify({confirm: true})}, 5000);
    const result = await response.json().catch(() => null);
    if (!response.ok) throw Error(requestError(result, `The shutdown request failed (HTTP ${response.status}).`));
    if (response.status !== 202 || result?.accepted !== true) throw Error('The service did not confirm the shutdown request.');
    acceptShutdown();
  } catch (error) {
    $('#shutdown-dialog-error').textContent = `${error.message} The shutdown outcome is not confirmed. Check whether TicketFlow is still running; Stop-App.cmd can stop it.`;
    $('#shutdown-dialog-error').hidden = false;
  } finally {
    state.shutdownRequesting = false;
    $('#shutdown-confirm').disabled = false;
    $('#shutdown-confirm').textContent = 'Stop TicketFlow';
    $('#shutdown-cancel').disabled = false;
    updateFormState();
  }
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
  const focusedTraceId = document.activeElement?.dataset?.traceBookingId;
  $('#bookings-body').innerHTML = bookings.length ? bookings.map(booking => {
    const status = statuses.has(booking.status) ? booking.status : 'unknown';
    const statusLabel = status[0].toUpperCase() + status.slice(1);
    const detail = booking.rejection_reason || (booking.worker_id ? `Worker ${booking.worker_id}` : 'Waiting for a worker');
    return `<tr><td><span class="booking-id" title="${escape(booking.id)}">#${escape(shortId(booking.id))}</span><div class="booking-guest" title="${escape(booking.customer_name)}">${escape(booking.customer_name)}</div><button class="trace-button" type="button" data-trace-booking-id="${escape(booking.id)}" aria-controls="trace-section" aria-expanded="${booking.id === state.traceBookingId}">View trace<span class="sr-only"> for booking #${escape(shortId(booking.id))}</span></button></td><td><div class="booking-event" title="${escape(booking.event_title)}">${escape(booking.event_title)}</div><time class="booking-time" datetime="${escape(booking.created_at)}">${escape(timeLabel(booking.created_at))}</time></td><td>${formatNumber(booking.quantity)}</td><td><span class="status-pill status-${status}" title="${escape(detail)}">${escape(statusLabel)}</span>${booking.rejection_reason ? `<small class="rejection-detail">${escape(booking.rejection_reason)}</small>` : ''}</td></tr>`;
  }).join('') : '<tr><td colspan="4" class="table-empty"><span class="empty-ledger-icon" aria-hidden="true">▤</span>No bookings yet. Your first request starts a new story.</td></tr>';
  if (focusedTraceId) [...document.querySelectorAll('[data-trace-booking-id]')].find(button => button.dataset.traceBookingId === focusedTraceId)?.focus({preventScroll: true});
}

function renderWorkers() {
  const workers = state.dashboard?.workers ?? [];
  const online = workers.filter(worker => worker.online);
  const offline = workers.filter(worker => !worker.online);
  const historyOpen = $('#worker-history')?.open;
  const workerCard = worker => {
    const status = worker.online === false ? 'offline' : worker.status === 'busy' ? 'busy' : worker.status === 'idle' ? 'idle' : 'offline';
    const label = {busy: 'Processing', idle: 'Ready', offline: 'Offline'}[status];
    const task = status === 'busy' && worker.current_booking_id ? `Working on #${shortId(worker.current_booking_id)}` : status === 'offline' ? 'No recent heartbeat' : 'Listening for the next request';
    return `<article class="worker-card"><span class="worker-icon" aria-hidden="true">${status === 'busy' ? '↻' : '◫'}</span><div class="worker-details"><div class="worker-name">${escape(worker.id)}${state.connection && state.dashboard.coordination?.active && state.dashboard.coordination.worker_id === worker.id ? ' <span class="worker-leader-label">Leader</span>' : ''}</div><div class="worker-task">${escape(task)}${Number.isFinite(worker.processed_count) ? ` · ${formatNumber(worker.processed_count)} processed, all runs` : ''}</div><div class="worker-task" title="${escape(worker.process_id || 'Historical process ID not recorded')}">${Number.isSafeInteger(worker.logical_time) ? `Last reported L ${formatNumber(worker.logical_time)}` : 'Logical clock not recorded'}</div></div><span class="worker-state ${status}">${label}</span></article>`;
  };
  $('#workers-list').innerHTML = (online.length ? online.map(workerCard).join('') : '<p class="empty-note">No workers are online. Requests remain queued until a worker is available.</p>') + (offline.length ? `<details class="worker-history" id="worker-history" ${historyOpen ? 'open' : ''}><summary>${offline.length} offline worker${offline.length === 1 ? '' : 's'}</summary><div class="workers-history-list">${offline.map(workerCard).join('')}</div></details>` : '');
}

function preciseTime(value) {
  const date = new Date(value);
  return value && !Number.isNaN(date.getTime()) ? date.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit', second: '2-digit'}) : 'Unavailable';
}

function renderCoordination() {
  const coordination = state.dashboard?.coordination;
  const badge = $('#leader-state');
  if (!coordination || typeof coordination.active !== 'boolean') {
    badge.textContent = 'Unavailable';
    badge.className = 'leader-state is-waiting';
    $('#leader-detail').innerHTML = '<p class="empty-note">The API has not supplied election information.</p>';
    return;
  }
  const active = state.connection && coordination.active;
  const processOpen = $('#leader-process')?.open;
  badge.textContent = !state.connection ? 'Last received' : active ? 'Lease active' : 'Awaiting election';
  badge.className = `leader-state ${active ? 'is-active' : 'is-waiting'}`;
  $('#leader-detail').innerHTML = `<dl class="leader-facts"><div><dt>Worker</dt><dd class="leader-worker">${escape(coordination.worker_id || 'No current leader')}</dd></div><div><dt>Election epoch</dt><dd>${formatNumber(coordination.epoch)}</dd></div><div><dt>Lease expires</dt><dd><time datetime="${escape(coordination.lease_until || '')}">${escape(preciseTime(coordination.lease_until))}</time></dd></div><div><dt>Expired jobs recovered</dt><dd>${formatNumber(coordination.recovered_total)}</dd></div></dl>${coordination.owner_id ? `<details class="leader-process" id="leader-process" ${processOpen ? 'open' : ''}><summary>Leader process run</summary><code>${escape(coordination.owner_id)}</code></details>` : ''}`;
  const history = Array.isArray(coordination.history) ? coordination.history : [];
  if (Number.isSafeInteger(coordination.api_logical_time)) {
    $('#leader-detail').insertAdjacentHTML('beforeend', `<p class="worker-task" title="${escape(coordination.api_process_id || '')}">API logical clock: L ${formatNumber(coordination.api_logical_time)} · ticks, not seconds</p>`);
  }
  $('#election-events').innerHTML = history.length ? history.slice(0, 5).map(event => `<li><strong>${escape(event.kind === 'elected' ? 'Leader elected' : event.kind === 'released' ? 'Lease released' : event.kind)}</strong><span>Epoch ${formatNumber(event.epoch)} · ${escape(event.worker_id || 'Unknown worker')}</span><time datetime="${escape(event.created_at || '')}">${escape(preciseTime(event.created_at))}</time></li>`).join('') : '<li class="empty-note">No election history yet.</li>';
}

function renderTrace() {
  const selected = state.traceBookingId;
  $('#trace-close').hidden = !selected;
  $('#trace-footnote').hidden = !selected;
  $('#trace-content').setAttribute('aria-busy', String(state.traceLoading));
  if (!selected) {
    $('#trace-status').textContent = 'Select View trace beside a booking to follow its API and worker events.';
    $('#trace-content').innerHTML = '';
    return;
  }
  if (state.traceError) {
    $('#trace-status').textContent = `Trace for #${shortId(selected)}: ${state.traceError} Retrying automatically.`;
  } else {
    $('#trace-status').textContent = state.traceLoading ? `Loading trace for booking #${shortId(selected)}…` : `Booking #${shortId(selected)} · ${state.traceData?.instrumented ? 'Live logical-clock trace' : 'Historical booking'}`;
  }
  const trace = state.traceData;
  if (!trace || trace.booking_id !== selected) {
    $('#trace-content').innerHTML = '';
    return;
  }
  const eventLabels = {queued: 'API queued request', claimed: 'Worker received job', confirmed: 'Worker confirmed booking', rejected: 'Worker rejected booking', recovered: 'Leader recovered expired job', observed: 'API observed result'};
  const logical = value => Number.isSafeInteger(value) && value >= 0;
  const events = Array.isArray(trace.events) ? trace.events : [];
  $('#trace-content').innerHTML = `<p class="trace-booking-id">Request / booking ID <code>${escape(selected)}</code></p>${!trace.instrumented ? '<p class="trace-legacy">This booking predates logical-clock tracing. Its historical events have no Lamport timestamps; they are not reconstructed.</p>' : ''}<ol class="trace-events">${events.map((event, index) => `<li class="trace-event ${event.ephemeral ? 'trace-observation' : ''}"><span class="trace-step" aria-label="Event ${index + 1}">${index + 1}</span><div class="trace-event-content"><div class="trace-event-heading"><h3>${escape(eventLabels[event.kind] || event.kind || 'Booking event')}</h3><span class="logical-tick">${logical(event.logical_time) ? `L ${formatNumber(event.logical_time)}` : 'Uninstrumented'}</span></div><p>${escape(event.message || '')}</p><div class="trace-process"><span>Process run</span><code>${escape(event.process_id || 'Not recorded')}</code></div><div class="trace-event-meta">${logical(event.received_time) ? `<span>Received L ${formatNumber(event.received_time)} → local L ${formatNumber(event.logical_time)}</span>` : `<span>${logical(event.logical_time) ? 'Local / send event' : 'No logical timestamp recorded'}</span>`}${Number.isInteger(event.attempt) && event.attempt > 0 ? `<span>Attempt ${formatNumber(event.attempt)}</span>` : ''}${event.created_at ? `<time datetime="${escape(event.created_at)}">${escape(preciseTime(event.created_at))} <span class="sr-only">wall-clock time</span></time>` : ''}</div>${event.ephemeral ? '<small class="trace-read-note">Read-only observation for this API response</small>' : ''}</div></li>`).join('')}</ol>`;
}

async function refreshTrace(force = false) {
  if (shutdownLocked()) return;
  const bookingId = state.traceBookingId;
  if (!bookingId || (!force && state.fetchingTraceId === bookingId)) return;
  const request = ++state.traceRequest;
  state.fetchingTraceId = bookingId;
  state.traceLoading = !state.traceData;
  if (state.traceLoading) renderTrace();
  try {
    const response = await fetchWithTimeout(`/api/bookings/${encodeURIComponent(bookingId)}/trace`, {cache: 'no-store'});
    const data = await response.json().catch(() => null);
    if (!response.ok) throw Error(requestError(data, `Trace unavailable (HTTP ${response.status}).`));
    if (!data || data.booking_id !== bookingId || !Array.isArray(data.events)) throw Error('The trace response was incomplete.');
    // Switching requests while a read is in flight must never show another booking's trace.
    if (state.shutdownAccepted || request !== state.traceRequest || bookingId !== state.traceBookingId) return;
    state.traceData = data;
    state.traceError = '';
  } catch (error) {
    if (request !== state.traceRequest || bookingId !== state.traceBookingId) return;
    state.traceError = error.message;
  } finally {
    if (request === state.traceRequest && bookingId === state.traceBookingId) {
      state.traceLoading = false;
      state.fetchingTraceId = '';
      renderTrace();
    }
  }
}

function renderActivity() {
  const activity = state.dashboard.activity ?? [];
  $('#activity-list').innerHTML = activity.length ? activity.slice(0, 6).map(item => `<li class="activity-item">${escape(item.message)}<small>${escape(item.worker_id || 'Booking service')}${item.created_at ? ` · ${escape(relativeTime(item.created_at))}` : ''}</small></li>`).join('') : '<li class="empty-note">No activity yet. Queue a request to follow it through the system.</li>';
}

function updateFormState() {
  const locked = state.submitting || Boolean(state.pendingIntent) || shutdownLocked();
  const event = eventList().find(item => item.id === state.selectedEventId);
  $('#event-select').disabled = locked || eventList().length === 0;
  $('#guest-alias').disabled = locked;
  $('#ticket-quantity').disabled = locked;
  $('#quantity-decrease').disabled = locked || Number($('#ticket-quantity').value) <= 1;
  $('#quantity-increase').disabled = locked || Number($('#ticket-quantity').value) >= 6;
  $('#booking-submit').disabled = shutdownLocked() || state.submitting || (!state.pendingIntent && (!state.connection || !event || event.available_seats === 0));
  $('#booking-submit').firstElementChild.textContent = state.submitting ? 'Sending request…' : state.pendingIntent ? 'Retry the same request' : 'Request tickets';
  document.querySelectorAll('[data-event-id]').forEach(button => {
    const item = eventList().find(event => event.id === button.dataset.eventId);
    button.disabled = locked || item?.available_seats === 0;
  });
  $('#include-demo').disabled = shutdownLocked();
  $('#trace-close').disabled = shutdownLocked();
  document.querySelectorAll('[data-trace-booking-id]').forEach(button => { button.disabled = shutdownLocked(); });
  renderShutdownControl();
}

function requestError(data, fallback) {
  if (typeof data?.detail === 'string') return data.detail;
  if (Array.isArray(data?.detail)) return data.detail.map(item => `${(item.loc ?? []).filter(part => part !== 'body').join(' ')}: ${item.msg || 'Invalid value'}`).join('. ');
  return fallback;
}

async function submitIntent() {
  if (shutdownLocked() || state.submitting || !state.pendingIntent) return;
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
  if (shutdownLocked() || state.submitting) return;
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
  if (!button || state.pendingIntent || state.submitting || shutdownLocked()) return;
  state.selectedEventId = button.dataset.eventId;
  showFormError('');
  renderEvents();
  $('#guest-alias').focus({preventScroll: true});
  if (matchMedia('(max-width: 630px)').matches) $('.booking-panel').scrollIntoView({behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth', block: 'start'});
});

function changeQuantity(amount) {
  if (shutdownLocked()) return;
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
  if (state.shutdownAccepted) return;
  const outstanding = allBookings().some(booking => !terminal(booking));
  state.timer = setTimeout(refreshDashboard, outstanding || state.submitting ? 1000 : 2000);
}

async function refreshDashboard() {
  if (state.shutdownAccepted) return;
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
    if (state.shutdownAccepted || requestedScope !== state.includeDemo) return;
    state.dashboard = data;
    $('#data-scope-note').textContent = state.includeDemo ? 'Metrics, ledger and activity include demo runs. Featured events stay unchanged.' : 'Showing showcase bookings. Background demo runs are excluded.';
    await refreshOwnBookings();
    if (state.shutdownAccepted) return;
    const workers = (data.workers ?? []).filter(worker => worker.online).length;
    setConnection(true, `API live · ${workers} worker${workers === 1 ? '' : 's'} online`);
    $('#connection-error').hidden = true;
    renderMetrics();
    renderEvents();
    renderBookings();
    renderWorkers();
    renderCoordination();
    renderActivity();
    await refreshTrace();
  } catch (error) {
    if (state.shutdownAccepted) return;
    setConnection(false, 'Connection interrupted');
    renderCoordination();
    renderWorkers();
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
    if (state.shutdownAccepted) { state.refreshAgain = false; }
    else if (state.refreshAgain) { state.refreshAgain = false; state.timer = setTimeout(refreshDashboard, 0); }
    else scheduleRefresh();
  }
}

if (state.pendingIntent) showFormError('A previous request has an unknown outcome. Retry the same request to recover its status safely.');
updateFormState();
void refreshDashboard();
void refreshControl();
document.addEventListener('visibilitychange', () => { if (!document.hidden && !state.shutdownAccepted) { void refreshDashboard(); void refreshControl(); } });
$('#bookings-body').addEventListener('click', event => {
  const button = event.target.closest('[data-trace-booking-id]');
  if (!button || shutdownLocked()) return;
  state.traceBookingId = button.dataset.traceBookingId;
  state.traceData = null;
  state.traceError = '';
  state.traceLoading = true;
  renderBookings();
  renderTrace();
  $('#trace-title').focus({preventScroll: true});
  $('#trace-section').scrollIntoView({behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth', block: 'start'});
  void refreshTrace(true);
});
$('#trace-close').addEventListener('click', () => {
  if (shutdownLocked()) return;
  const previous = state.traceBookingId;
  state.traceBookingId = '';
  state.traceData = null;
  state.traceError = '';
  state.traceLoading = false;
  state.fetchingTraceId = '';
  state.traceRequest += 1;
  renderTrace();
  renderBookings();
  const previousButton = [...document.querySelectorAll('[data-trace-booking-id]')].find(button => button.dataset.traceBookingId === previous);
  (previousButton || $('#bookings-title')).focus({preventScroll: true});
});
$('#include-demo').addEventListener('change', event => {
  if (shutdownLocked()) return;
  state.includeDemo = event.target.checked;
  $('#data-scope-note').textContent = 'Updating the dashboard scope…';
  void refreshDashboard();
});

$('#shutdown-button').addEventListener('click', () => {
  if (shutdownLocked() || state.submitting || state.control?.enabled !== true) return;
  $('#shutdown-dialog-error').hidden = true;
  $('#shutdown-dialog').showModal();
  $('#shutdown-cancel').focus();
});
$('#shutdown-cancel').addEventListener('click', () => $('#shutdown-dialog').close());
$('#shutdown-dialog').addEventListener('cancel', event => { if (state.shutdownRequesting) event.preventDefault(); });
$('#shutdown-confirm').addEventListener('click', () => void requestShutdown());
