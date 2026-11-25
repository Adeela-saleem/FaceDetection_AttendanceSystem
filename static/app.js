/* Shared helpers for every page: API calls, layout/nav, dialogs, toasts and the browser camera. */
const $ = (s, r = document) => r.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const sleep = ms => new Promise(r => setTimeout(r, ms));
const todayStr = () => { const d = new Date(); return new Date(d - d.getTimezoneOffset() * 6e4).toISOString().slice(0, 10); };
const initials = n => String(n).split(/\s+/).slice(0, 2).map(w => w[0] || '').join('').toUpperCase();
let ME = null, SETTINGS = null;

/* ---------- API ---------- */
async function api(path, {method = 'GET', body, noRedirect} = {}) {
  const res = await fetch(path, {
    method, credentials: 'same-origin',
    headers: body ? {'Content-Type': 'application/json'} : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  if (res.status === 401 && !noRedirect) { location.href = '/login'; return new Promise(() => {}); }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    let msg = data.detail || `Error ${res.status}`;
    if (Array.isArray(msg)) msg = msg.map(d => `${d.loc[d.loc.length - 1]}: ${d.msg}`).join(', ');
    throw new Error(msg);
  }
  return data;
}

/* ---------- toasts + dialogs ---------- */
function toast(msg, kind = '') {
  let box = $('#toasts');
  if (!box) { box = document.createElement('div'); box.id = 'toasts'; document.body.append(box); }
  const t = document.createElement('div');
  t.className = 'toast ' + kind; t.textContent = msg; box.append(t);
  setTimeout(() => t.remove(), kind === 'bad' || kind === 'warn' ? 7000 : 3000);
}

/** Modal form. onSubmit(formElement) may throw -> error is shown inside the dialog. */
function dialog({title, html, submit = 'Save', danger = false, onSubmit}) {
  const d = document.createElement('dialog');
  d.innerHTML = `<form method="dialog"><h2>${esc(title)}</h2><div class="form">${html}</div>
    <div class="notice bad hidden" style="margin:12px 0 0"></div>
    <div class="foot"><button type="button" class="btn ghost" data-x>Cancel</button>
    <button class="btn ${danger ? 'danger' : ''}">${esc(submit)}</button></div></form>`;
  document.body.append(d);
  const form = $('form', d), err = $('.notice', d), btn = $('.foot .btn:last-child', d);
  $('[data-x]', d).onclick = () => d.close();
  d.addEventListener('close', () => d.remove());
  form.onsubmit = async e => {
    e.preventDefault(); err.classList.add('hidden'); btn.disabled = true;
    try { await onSubmit(form); d.close(); }
    catch (x) { err.textContent = x.message; err.classList.remove('hidden'); }
    finally { btn.disabled = false; }
  };
  d.showModal();
  const first = $('input,select', d); if (first) first.focus();
  return d;
}
const confirmBox = (title, message, submit = 'Delete') =>
  new Promise(res => {
    let done = false;
    const d = dialog({title, html: `<p class="full" style="margin:0">${esc(message)}</p>`, submit, danger: true,
      onSubmit: async () => { done = true; res(true); }});
    d.addEventListener('close', () => { if (!done) res(false); });
  });

/* ---------- layout ---------- */
const NAV = [
  ['/', '📊', 'Dashboard', () => true],
  ['/attendance', '📷', 'Take Attendance', m => m.can.take_attendance],
  ['/students', '👩‍🎓', 'Students', () => true],
  ['/sections', '🏫', 'Sections', () => true],
  ['/register', '➕', 'Register Student', m => m.can.manage_students],
  ['/records', '🗂', 'Records', () => true],
  ['/users', '👥', 'Users', m => m.can.manage_users],
  ['/settings', '⚙️', 'Settings', m => m.can.edit_settings],
];

/** Loads the logged-in user + settings, draws sidebar/topbar, returns the <main> element. */
async function initPage({title, need} = {}) {
  ME = await api('/api/auth/me');
  SETTINGS = await api('/api/settings');
  if (need && !ME.can[need]) { location.href = '/'; return new Promise(() => {}); }
  document.title = `${title} · ${SETTINGS.institute_name}`;
  const path = location.pathname;
  document.body.innerHTML = `
    <div class="shell">
      <aside class="side" id="side">
        <div class="brand">🎓 <span>${esc(SETTINGS.institute_name)}</span></div>
        ${NAV.filter(n => n[3](ME)).map(n =>
          `<a class="nav ${n[0] === path ? 'on' : ''}" href="${n[0]}"><span>${n[1]}</span>${n[2]}</a>`).join('')}
        <div class="me"><b>${esc(ME.full_name)}</b><span>${esc(ME.role_label)}</span>
          <button class="btn ghost sm" id="logout">Logout</button></div>
      </aside>
      <div class="main">
        <header class="top"><button class="burger" id="burger">☰</button><h1>${esc(title)}</h1></header>
        <main id="content"></main>
      </div>
    </div>`;
  $('#burger').onclick = () => $('#side').classList.toggle('open');
  $('#logout').onclick = async () => { await api('/api/auth/logout', {method: 'POST'}); location.href = '/login'; };
  return $('#content');
}

const fmtWindow = s => s.class_start && s.class_end ? `${s.class_start}–${s.class_end}` : 'Time not set';
/** Simple label for class time window. */
const lateRule = s => {
  if (!(s.class_start && s.class_end)) return 'No class time — attendance anytime';
  return `Class: ${s.class_start}–${s.class_end}`;
};
const statusBadge = s => `<span class="badge ok">Present</span>`;
const avatarHtml = s => s.has_avatar
  ? `<img class="avatar" src="/api/students/${s.id}/avatar" alt="">`
  : `<span class="avatar">${esc(initials(s.name))}</span>`;

/* ---------- browser camera ---------- */
function cameraErrorMessage(e) {
  switch (e && e.name) {
    case 'NotAllowedError': case 'SecurityError':
      return 'Camera permission is blocked. Click the camera icon in the address bar, select "Allow", then reload the page.';
    case 'NotFoundError': case 'OverconstrainedError':
      return 'No camera was found. Is the camera connected?';
    case 'NotReadableError': case 'AbortError':
      return 'The camera is busy. Another app or tab may be using it (including recognize_live.py or Zoom/Meet). Close it and try again.';
    default:     return 'The camera could not start: ' + (e && e.message || e);
  }
}

/** Starts the camera. Always resolves with a stream or rejects with a readable message (never hangs silently). */
async function startCamera(video) {
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    throw new Error(window.isSecureContext
      ? 'This browser does not support camera access.'
      : `Camera access is allowed only over HTTPS or http://localhost. You are opening ${location.origin}; use localhost or enable HTTPS.`);
  }
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({
      video: {width: {ideal: 1280}, height: {ideal: 720}, facingMode: 'user'}, audio: false});
  } catch (e) { throw new Error(cameraErrorMessage(e)); }
  video.srcObject = stream; video.muted = true; video.playsInline = true;
  try {
    await video.play();
    const t0 = Date.now();
    while (!video.videoWidth) {                      // wait for the first real frame
      if (Date.now() - t0 > 8000) throw new Error('no frames');
      await sleep(100);
    }
  } catch (e) {
    stopCamera(stream, video);
    throw new Error('The camera opened, but no video frame arrived. Another app may be using it; close it and try again.');
  }
  return stream;
}
function stopCamera(stream, video) {
  if (stream) stream.getTracks().forEach(t => t.stop());
  if (video) video.srcObject = null;
}
function captureFrame(video, maxSide = 800, quality = 0.85) {
  const s = Math.min(1, maxSide / Math.max(video.videoWidth, video.videoHeight));
  const c = document.createElement('canvas');
  c.width = Math.round(video.videoWidth * s); c.height = Math.round(video.videoHeight * s);
  c.getContext('2d').drawImage(video, 0, 0, c.width, c.height);
  return c.toDataURL('image/jpeg', quality);
}
/** Reads an image File, fixes phone rotation, shrinks it, returns a JPEG data URL. */
async function fileToDataURL(file, maxSide = 1000) {
  let bmp;
  try { bmp = await createImageBitmap(file, {imageOrientation: 'from-image'}); }
  catch { throw new Error(`${file.name}: This image format could not be opened. Use JPG or PNG.`); }
  const s = Math.min(1, maxSide / Math.max(bmp.width, bmp.height));
  const c = document.createElement('canvas');
  c.width = Math.round(bmp.width * s); c.height = Math.round(bmp.height * s);
  c.getContext('2d').drawImage(bmp, 0, 0, c.width, c.height);
  return c.toDataURL('image/jpeg', 0.9);
}
/** Draws recognition boxes + labels on a canvas whose size equals the scanned image. */
function drawFaces(canvas, size, faces) {
  canvas.width = size.w; canvas.height = size.h;
  const g = canvas.getContext('2d'), lw = Math.max(2, size.w / 250), fs = Math.max(13, size.w / 40);
  g.clearRect(0, 0, size.w, size.h); g.font = `600 ${fs}px system-ui`; g.lineWidth = lw;
  for (const f of faces) {
    const [t, r, b, l] = f.box;
    const color = !f.student_id ? '#dc2626' : (f.state === 'confirming' ? '#d97706' : '#16a34a');
    const label = f.student_id ? `${f.name}${f.state === 'confirming' ? ` ${f.progress[0]}/${f.progress[1]}` : ''}` : 'Unknown';
    g.strokeStyle = color; g.strokeRect(l, t, r - l, b - t);
    const w = g.measureText(label).width + 12;
    g.fillStyle = color; g.fillRect(l, Math.max(t - fs - 8, 0), w, fs + 8);
    g.fillStyle = '#fff'; g.fillText(label, l + 6, Math.max(t - 8, fs));
  }
}

addEventListener('unhandledrejection', e => { if (e.reason && e.reason.message) toast(e.reason.message, 'bad'); });
