const state = { csrf: sessionStorage.getItem('together-csrf'), username: null, displayName: null, room: null, rooms: [], roomActivity: new Map(), lastUserMessage: null, files: [], galleryShowRemoved: false, galleryLoadGeneration: 0, followLatest: true, scrollFrame: 0, seenMessageIds: new Set(), roomGeneration: 0, loadingMessages: false, pollingMessages: false, activityPolling: false, sendingMessage: false };
const MESSAGE_POLL_INTERVAL_MS = 2500;
const ROOM_ACTIVITY_POLL_INTERVAL_MS = 5000;
const $ = (selector) => document.querySelector(selector);

function isNearLatest() {
  const page = document.scrollingElement;
  return !page || page.scrollHeight - page.clientHeight - page.scrollTop <= 120;
}

function scheduleLatestScroll(force = false) {
  if (force) state.followLatest = true;
  if (!state.followLatest || state.scrollFrame) return;
  state.scrollFrame = requestAnimationFrame(() => {
    state.scrollFrame = 0;
    if (state.followLatest && document.scrollingElement) {
      window.scrollTo({ top: document.scrollingElement.scrollHeight, behavior: 'auto' });
    }
  });
}

window.addEventListener('scroll', () => {
  state.followLatest = isNearLatest();
}, { passive: true });
window.addEventListener('resize', () => scheduleLatestScroll());

async function request(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (state.csrf && options.method && options.method !== 'GET') headers['X-Together-CSRF'] = state.csrf;
  if (options.body && !(options.body instanceof FormData)) headers['Content-Type'] = 'application/json';
  const response = await fetch(path, { ...options, headers });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new Error(payload.detail || 'Something went wrong.');
  }
  return response.status === 204 ? null : response.json();
}

function setLoggedIn(isLoggedIn) {
  $('#loginView').hidden = isLoggedIn;
  $('#roomView').hidden = !isLoggedIn;
}

function putPromptInComposer(content, status) {
  $('#messageInput').value = content;
  $('#composerStatus').textContent = status;
  $('#composerStatus').removeAttribute('aria-hidden');
  $('#messageInput').focus();
}

function retryPromptNow(content) {
  if (state.sendingMessage) {
    $('#composerStatus').textContent = 'Pip is still responding; try again when the reply finishes.';
    return;
  }
  const hasDraft = $('#messageInput').value.trim() || state.files.length;
  if (hasDraft && !window.confirm('Retrying replaces your current draft and its attached images. Continue?')) return;
  for (const file of state.files) URL.revokeObjectURL(file.url);
  state.files = [];
  paintSelectedFiles();
  $('#messageInput').value = content;
  $('#composer').requestSubmit();
}

function messageElement(message, retryPrompt = null) {
  const item = document.createElement('li');
  if (message.id !== undefined && message.id !== null) item.dataset.messageId = String(message.id);
  const author = String(message.author || '').trim();
  const speaker = message.role === 'assistant'
    ? 'assistant'
    : state.displayName && author.toLowerCase() === state.displayName.toLowerCase()
      ? 'self'
      : 'other';
  item.className = `message ${message.role} message-${speaker}`;
  const meta = document.createElement('div');
  meta.className = 'message-meta';
  meta.textContent = message.role === 'assistant' ? 'Pip' : message.author;
  item.append(meta);
  const attachments = message.attachments || (message.attachment ? [message.attachment] : []);
  for (const [index, attachment] of attachments.entries()) {
    const link = document.createElement('a');
    link.className = 'message-image-link';
    link.href = attachment.url;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    link.setAttribute('aria-label', `Open shared image attachment ${index + 1} in a new tab`);
    const img = document.createElement('img');
    img.src = attachment.url;
    img.alt = `Shared image attachment ${index + 1}`;
    img.loading = 'lazy';
    img.addEventListener('load', () => scheduleLatestScroll());
    link.append(img);
    item.append(link);
  }
  const body = document.createElement('p');
  body.className = 'message-body';
  body.textContent = message.content;
  item.append(body);
  const actions = document.createElement('div');
  actions.className = 'message-actions';
  if (message.role === 'user' && state.displayName && author.toLowerCase() === state.displayName.toLowerCase()) {
    const edit = document.createElement('button');
    edit.type = 'button';
    edit.className = 'message-action';
    edit.textContent = 'Edit & resend';
    edit.addEventListener('click', () => putPromptInComposer(
      message.content,
      message.attachments?.length
        ? 'Editing a copy; the original stays in the thread. Re-add its image from Gallery if needed.'
        : 'Editing a copy; the original message stays in the thread.'
    ));
    actions.append(edit);
  } else if (message.role === 'assistant' && retryPrompt?.content) {
    const retry = document.createElement('button');
    retry.type = 'button';
    retry.className = 'message-action';
    retry.textContent = 'Retry';
    retry.setAttribute('aria-label', 'Retry Pip response by sending the previous prompt again as a new message');
    retry.addEventListener('click', () => retryPromptNow(retryPrompt.content));
    actions.append(retry);
  }
  if (actions.childElementCount) item.append(actions);
  return item;
}

function typingElement() {
  const item = document.createElement('li');
  item.className = 'message assistant typing-indicator';
  item.setAttribute('role', 'status');
  item.setAttribute('aria-label', 'Pip is typing');
  item.innerHTML = '<div class="typing-line"><span class="message-meta">Pip</span><span class="typing-dots" aria-hidden="true"><span></span><span></span><span></span></span></div>';
  return item;
}

function removeTypingIndicator() {
  document.querySelector('.typing-indicator')?.remove();
}

async function loadMessages() {
  if (!state.room) return;
  const roomId = state.room.id;
  const generation = state.roomGeneration;
  state.loadingMessages = true;
  try {
    const messages = await request(`/api/rooms/${roomId}/messages`);
    if (state.room?.id !== roomId || state.roomGeneration !== generation) return;
    state.seenMessageIds = new Set(messages.map((message) => String(message.id)));
    markRoomRead(roomId, messages.length ? String(messages[messages.length - 1].id) : null);
    let latestUserMessage = null;
    const rendered = messages.map((message) => {
      const element = messageElement(message, message.role === 'assistant' ? latestUserMessage : null);
      if (message.role === 'user') latestUserMessage = message;
      return element;
    });
    state.lastUserMessage = latestUserMessage;
    $('#messages').replaceChildren(...rendered);
    $('#emptyState').hidden = messages.length > 0;
    scheduleLatestScroll();
  } finally {
    if (state.roomGeneration === generation) state.loadingMessages = false;
  }
}

async function pollRoomMessages() {
  if (!state.room || state.sendingMessage || state.loadingMessages || state.pollingMessages || document.visibilityState !== 'visible') return;
  const roomId = state.room.id;
  const generation = state.roomGeneration;
  state.pollingMessages = true;
  try {
    const messages = await request(`/api/rooms/${roomId}/messages`);
    if (state.room?.id !== roomId || state.roomGeneration !== generation) return;
    const unseen = messages.filter((message) => !state.seenMessageIds.has(String(message.id)));
    for (const message of unseen) {
      state.seenMessageIds.add(String(message.id));
      if (message.role === 'user') state.lastUserMessage = message;
      $('#messages').append(messageElement(message, message.role === 'assistant' ? state.lastUserMessage : null));
    }
    if (unseen.length) {
      $('#emptyState').hidden = true;
      scheduleLatestScroll();
    }
  } catch {
    // Transient network errors are retried by the next poll; don't disrupt composing.
  } finally {
    state.pollingMessages = false;
  }
}

function roomButton(room) {
  const button = document.createElement('button');
  button.type = 'button';
  const title = document.createElement('span');
  title.textContent = room.title;
  button.append(title);
  const latestMessageId = state.roomActivity.get(room.id) ?? null;
  let lastSeen = getLastSeenMessageId(room.id);
  if (lastSeen === null) {
    lastSeen = latestMessageId;
    if (latestMessageId !== null) setLastSeenMessageId(room.id, latestMessageId);
  }
  if (room.id === state.room?.id && latestMessageId !== null) {
    lastSeen = latestMessageId;
    setLastSeenMessageId(room.id, latestMessageId);
  }
  if (latestMessageId !== null && lastSeen !== latestMessageId) {
    const badge = document.createElement('span');
    badge.className = 'room-activity-badge';
    badge.textContent = 'New';
    badge.setAttribute('aria-label', 'New activity');
    button.append(badge);
  }
  button.classList.toggle('active', state.room?.id === room.id);
  button.addEventListener('click', () => selectRoom(room));
  return button;
}

function lastSeenStorageKey(roomId) {
  return `together:last-seen:${state.username}:${roomId}`;
}

function getLastSeenMessageId(roomId) {
  return state.username ? localStorage.getItem(lastSeenStorageKey(roomId)) : null;
}

function setLastSeenMessageId(roomId, messageId) {
  if (state.username && messageId !== null) localStorage.setItem(lastSeenStorageKey(roomId), messageId);
}

function markRoomRead(roomId, latestMessageId) {
  if (latestMessageId !== null) {
    state.roomActivity.set(roomId, latestMessageId);
    setLastSeenMessageId(roomId, latestMessageId);
  }
  renderRoomLists();
}

function renderRoomLists() {
  const shared = state.rooms.filter((room) => room.visibility === 'shared');
  const privateRooms = state.rooms.filter((room) => room.visibility === 'private');
  $('#roomList').replaceChildren(...shared.map(roomButton));
  $('#privateRoomList').replaceChildren(...privateRooms.map(roomButton));
}

async function refreshRoomActivity() {
  if (!state.username || state.activityPolling || document.visibilityState !== 'visible') return;
  state.activityPolling = true;
  try {
    const activity = await request('/api/rooms/activity');
    for (const room of activity) {
      const latest = room.latestMessageId;
      state.roomActivity.set(room.id, latest);
      if (room.id === state.room?.id && latest !== null) setLastSeenMessageId(room.id, latest);
      else if (getLastSeenMessageId(room.id) === null && latest !== null) setLastSeenMessageId(room.id, latest);
    }
    renderRoomLists();
  } catch {
    // Activity cues are best-effort; the next interval retries transient errors.
  } finally {
    state.activityPolling = false;
  }
}

async function loadRooms() {
  const rooms = await request('/api/rooms');
  state.rooms = rooms;
  renderRoomLists();
  await refreshRoomActivity();
  if (!state.room) {
    const key = lastRoomStorageKey(state.username);
    const savedRoomId = state.username ? sessionStorage.getItem(key) : null;
    const savedRoom = rooms.find((room) => room.id === savedRoomId);
    if (savedRoom) selectRoom(savedRoom);
    else {
      if (savedRoomId) sessionStorage.removeItem(key);
      showRoomWelcome();
    }
  }
}

function lastRoomStorageKey(username) {
  return `together:last-room:${username}`;
}

function showRoomWelcome() {
  if (state.room) state.roomGeneration += 1;
  state.room = null;
  state.seenMessageIds.clear();
  $('#roomTitle').textContent = 'Choose a room';
  $('#roomVisibility').textContent = 'Your spaces';
  $('#manageRoomButton').disabled = true;
  $('#messages').replaceChildren();
  $('#emptyEyebrow').textContent = 'Welcome back';
  $('#emptyTitle').textContent = 'Pick up where you left off';
  $('#emptyCopy').textContent = 'Choose a conversation from your rooms, or start a new one.';
  $('#emptyState').hidden = false;
  $('#composerStatus').textContent = 'Choose a conversation to begin';
  $('#composerStatus').removeAttribute('aria-hidden');
  $('#messageInput').disabled = true;
  $('#sendButton').disabled = true;
  $('#imageInput').disabled = true;
  clearImageSelection();
}

function selectRoom(room) {
  const changedRoom = state.room?.id !== room.id;
  state.room = room;
  if (state.username) sessionStorage.setItem(lastRoomStorageKey(state.username), room.id);
  if (changedRoom) {
    state.followLatest = true;
    state.roomGeneration += 1;
    state.seenMessageIds.clear();
    state.lastUserMessage = null;
  }
  $('#roomTitle').textContent = room.title;
  const isPrivate = room.visibility === 'private';
  $('#roomVisibility').textContent = isPrivate ? 'Private to me' : 'Shared space';
  setComposerStatus();
  $('#manageRoomButton').disabled = false;
  $('#messageInput').disabled = false;
  $('#sendButton').disabled = false;
  $('#imageInput').disabled = false;
  clearImageSelection(); // an image picked for one thread must not follow into another
  setEmptyState(isPrivate);
  loadRooms();
  return loadMessages();
}

function setEmptyState(isPrivate) {
  $('#emptyEyebrow').textContent = isPrivate ? 'Just for you' : 'Begin anywhere';
  $('#emptyTitle').textContent = isPrivate ? 'Your private space' : 'What are you making space for?';
  $('#emptyCopy').textContent = isPrivate
    ? 'Other participants cannot open this room. Pip’s persistent profile memory is shared across participants, so avoid sensitive details here until memory isolation is enforced.'
    : 'Share a thought, an image, a sketch—then work it into something useful together.';
}

function setComposerStatus() {
  const isPrivate = state.room?.visibility === 'private';
  $('#composerStatus').removeAttribute('aria-hidden');
  $('#composerStatus').textContent = isPrivate
    ? 'Private room; Pip’s profile memory is shared'
    : 'Shared with participants';
}

async function openTogether() {
  const me = await request('/api/me');
  state.username = me.username;
  state.displayName = me.displayName;
  $('#memberName').textContent = me.displayName;
  setLoggedIn(true);
  await loadRooms();
}

$('#loginForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  const error = $('#loginError');
  error.hidden = true;
  const form = new FormData(event.currentTarget);
  try {
    const result = await request('/api/auth/login', {
      method: 'POST', body: JSON.stringify(Object.fromEntries(form))
    });
    state.csrf = result.csrfToken;
    sessionStorage.setItem('together-csrf', state.csrf);
    await openTogether();
  } catch (err) {
    error.textContent = err.message;
    error.hidden = false;
  }
});

function clearImageSelection() {
  for (const selected of state.files) URL.revokeObjectURL(selected.url);
  state.files = [];
  $('#imageInput').value = '';
  $('#imageThumbnails').replaceChildren();
  $('#imagePreview').hidden = true;
}

function addSelectedFiles(files, announceLimit = true) {
  const available = 4 - state.files.length;
  const accepted = files.slice(0, Math.max(0, available));
  for (const file of accepted) state.files.push({ file, url: URL.createObjectURL(file) });
  if (announceLimit && accepted.length < files.length) {
    $('#composerStatus').textContent = 'You can attach up to four images per message.';
    $('#composerStatus').removeAttribute('aria-hidden');
  }
  paintSelectedFiles();
}

function paintSelectedFiles() {
  const thumbnails = $('#imageThumbnails');
  thumbnails.replaceChildren();
  state.files.forEach((selected, index) => {
    const item = document.createElement('div');
    item.className = 'selected-image';
    const image = document.createElement('img');
    image.src = selected.url;
    image.alt = `Preview of image ${index + 1}`;
    const label = document.createElement('span');
    label.textContent = `Image ${index + 1}`;
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.textContent = 'Remove';
    remove.setAttribute('aria-label', `Remove image ${index + 1}`);
    remove.addEventListener('click', () => {
      URL.revokeObjectURL(state.files[index].url);
      state.files.splice(index, 1);
      paintSelectedFiles();
    });
    item.append(image, label, remove);
    thumbnails.append(item);
  });
  $('#imagePreview').hidden = state.files.length === 0;
}

function galleryImageCard(image) {
  const card = document.createElement('article');
  card.className = 'gallery-card';
  const preview = document.createElement('img');
  preview.src = image.url;
  preview.alt = `Image from ${image.roomTitle}`;
  preview.loading = 'lazy';
  const source = document.createElement('p');
  source.className = 'gallery-source';
  source.textContent = image.roomTitle;
  const visibility = document.createElement('p');
  visibility.className = 'gallery-visibility';
  const date = new Date(image.createdAt);
  visibility.textContent = `${image.visibility === 'private' ? 'Private room' : 'Shared room'} · ${Number.isNaN(date.valueOf()) ? '' : date.toLocaleDateString()}`;
  const use = document.createElement('button');
  use.type = 'button';
  use.textContent = state.room ? 'Add to message' : 'Choose a room first';
  use.disabled = !state.room;
  use.addEventListener('click', async () => {
    if (state.files.length >= 4) {
      $('#galleryStatus').textContent = 'You can attach up to four images per message.';
      return;
    }
    if (image.visibility === 'private' && state.room?.visibility === 'shared' &&
        !window.confirm(`This image is from your private “${image.roomTitle}” room. Add it to “${state.room.title}”? Everyone in that shared room will be able to see it.`)) return;
    use.disabled = true;
    use.textContent = 'Adding…';
    try {
      const response = await fetch(image.url);
      if (!response.ok) throw new Error('Could not load that gallery image.');
      const blob = await response.blob();
      const type = blob.type || image.mimeType;
      const extension = type === 'image/jpeg' ? 'jpg' : type.split('/')[1] || 'png';
      const file = new File([blob], `gallery-${image.id.slice(0, 8)}.${extension}`, { type });
      addSelectedFiles([file]);
      $('#galleryStatus').textContent = 'Added to the composer. You can choose another image or close the gallery.';
      use.textContent = 'Added';
    } catch (error) {
      $('#galleryStatus').textContent = error.message || 'Could not add that image.';
      use.disabled = false;
      use.textContent = 'Try again';
    }
  });
  const galleryAction = document.createElement('button');
  galleryAction.type = 'button';
  galleryAction.className = image.hidden ? 'restore-gallery-button' : 'remove-gallery-button';
  galleryAction.textContent = image.hidden ? 'Restore' : 'Remove';
  galleryAction.setAttribute('aria-label', `${image.hidden ? 'Restore' : 'Remove'} image from ${image.roomTitle} ${image.hidden ? 'to' : 'from'} the Gallery`);
  galleryAction.addEventListener('click', async () => {
    if (!image.hidden && !window.confirm(
      `Hide this image from Gallery for everyone who can access “${image.roomTitle}”? It will stay visible in existing messages and can be restored from Show removed.`
    )) return;
    galleryAction.disabled = true;
    galleryAction.textContent = image.hidden ? 'Restoring…' : 'Removing…';
    try {
      const path = `/api/gallery/${encodeURIComponent(image.id)}${image.hidden ? '/restore' : ''}`;
      await request(path, { method: image.hidden ? 'POST' : 'DELETE' });
      if (image.hidden) {
        state.galleryShowRemoved = false;
        $('#toggleRemovedButton').textContent = 'Show removed';
        $('#toggleRemovedButton').setAttribute('aria-pressed', 'false');
      }
      await loadGallery();
      $('#galleryStatus').textContent = image.hidden
        ? 'Image restored to Gallery for everyone with access.'
        : 'Removed from Gallery for everyone with access. Existing messages still show the image; use Show removed to restore it.';
    } catch (error) {
      galleryAction.disabled = false;
      galleryAction.textContent = image.hidden ? 'Restore' : 'Remove';
      $('#galleryStatus').textContent = error.message || 'Could not update this gallery image.';
    }
  });
  const actions = document.createElement('div');
  actions.className = 'gallery-card-actions';
  actions.append(use, galleryAction);
  card.append(preview, source, visibility, actions);
  return card;
}

async function loadGallery() {
  const generation = ++state.galleryLoadGeneration;
  $('#galleryGrid').replaceChildren();
  $('#galleryStatus').textContent = `Loading ${state.galleryShowRemoved ? 'removed ' : ''}images…`;
  try {
    const path = state.galleryShowRemoved ? '/api/gallery?include_removed=true' : '/api/gallery';
    const images = await request(path);
    if (generation !== state.galleryLoadGeneration) return;
    if (!images.length) {
      $('#galleryStatus').textContent = state.galleryShowRemoved
        ? 'No removed images to restore.'
        : 'No images yet. Photos you add and images Pip creates will appear here.';
      return;
    }
    $('#galleryGrid').replaceChildren(...images.map(galleryImageCard));
    $('#galleryStatus').textContent = state.galleryShowRemoved
      ? `${images.length} removed image${images.length === 1 ? '' : 's'}. Restore any to return it to everyone’s Gallery.`
      : `${images.length} image${images.length === 1 ? '' : 's'} from rooms you can access.`;
  } catch (error) {
    if (generation === state.galleryLoadGeneration) {
      $('#galleryStatus').textContent = error.message || 'Could not load the gallery.';
    }
  }
}

async function openGallery() {
  $('#galleryNote').textContent = state.room
    ? `Browse photos and Pip’s creations from rooms you can access. Add one to “${state.room.title}”. Removing an image hides it from everyone’s Gallery but keeps it in old messages; restore it with Show removed.`
    : 'Browse photos and Pip’s creations from rooms you can access. Select a room to reuse an image. Removing one hides it from everyone’s Gallery but keeps it in old messages; restore it with Show removed.';
  $('#toggleRemovedButton').textContent = state.galleryShowRemoved ? 'Show current' : 'Show removed';
  $('#toggleRemovedButton').setAttribute('aria-pressed', String(state.galleryShowRemoved));
  $('#galleryDialog').showModal();
  await loadGallery();
}

$('#browseGalleryButton').addEventListener('click', openGallery);
$('#closeGalleryButton').addEventListener('click', () => $('#galleryDialog').close());
$('#toggleRemovedButton').addEventListener('click', async () => {
  state.galleryShowRemoved = !state.galleryShowRemoved;
  $('#toggleRemovedButton').textContent = state.galleryShowRemoved ? 'Show current' : 'Show removed';
  $('#toggleRemovedButton').setAttribute('aria-pressed', String(state.galleryShowRemoved));
  await loadGallery();
});

function searchResultElement(result) {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'search-result';
  const heading = document.createElement('span');
  heading.className = 'search-result-heading';
  heading.textContent = `${result.roomTitle} · ${result.author}`;
  const visibility = document.createElement('span');
  visibility.className = 'search-result-visibility';
  visibility.textContent = result.visibility === 'private' ? 'Private' : 'Shared';
  const excerpt = document.createElement('span');
  excerpt.className = 'search-result-excerpt';
  excerpt.textContent = result.content;
  button.append(heading, visibility, excerpt);
  button.addEventListener('click', () => openSearchResult(result));
  return button;
}

async function openSearchResult(result) {
  const room = state.rooms.find((item) => item.id === result.roomId);
  if (!room) return;
  try {
    await selectRoom(room);
    const message = Array.from($('#messages').children)
      .find((item) => item.dataset.messageId === result.messageId);
    if (!message) throw new Error('That message is no longer available. Search again.');
    state.followLatest = false;
    message.scrollIntoView({
      block: 'center',
      behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth'
    });
    message.classList.add('search-hit');
    window.setTimeout(() => message.classList.remove('search-hit'), 2200);
    $('#searchDialog').close();
  } catch (error) {
    $('#searchStatus').textContent = error.message || 'Could not open that message.';
  }
}

$('#searchMessagesButton').addEventListener('click', () => {
  $('#searchStatus').textContent = 'Search messages across the rooms you can access.';
  $('#searchDialog').showModal();
  $('#searchQuery').focus();
});
$('#closeSearchButton').addEventListener('click', () => $('#searchDialog').close());
$('#messageSearchForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  const query = $('#searchQuery').value.trim();
  $('#searchStatus').textContent = 'Searching…';
  $('#searchResults').replaceChildren();
  try {
    const results = await request(`/api/search?q=${encodeURIComponent(query)}`);
    $('#searchResults').replaceChildren(...results.map(searchResultElement));
    $('#searchStatus').textContent = results.length
      ? `${results.length} matching message${results.length === 1 ? '' : 's'}. Select one to jump to it.`
      : 'No matching messages found.';
  } catch (error) {
    $('#searchStatus').textContent = error.message || 'Search is unavailable right now.';
  }
});

$('#imageInput').addEventListener('change', (event) => {
  const files = Array.from(event.target.files || []);
  if (files.length) addSelectedFiles(files);
  event.target.value = '';
});

$('#imageInput').multiple = true;

$('#messageInput').addEventListener('keydown', (event) => {
  if (event.key !== 'Enter' || event.shiftKey || event.isComposing) return;
  event.preventDefault();
  if (!$('#sendButton').disabled) $('#composer').requestSubmit();
});

$('#composer').addEventListener('submit', async (event) => {
  event.preventDefault();
  const input = $('#messageInput');
  const button = $('#sendButton');
  const content = input.value.trim();
  if (!content || !state.room) return;
  const submittedFiles = state.files.map((selected) => selected.file);
  let userMessageReceived = false;
  const optimisticImageUrls = submittedFiles.map((file) => URL.createObjectURL(file));
  const optimisticMessage = messageElement({
    role: 'user',
    author: $('#memberName').textContent,
    content,
    ...(submittedFiles.length ? { attachments: optimisticImageUrls.map((url) => ({ url })) } : {})
  });
  button.disabled = true;
  state.sendingMessage = true;
  $('#composerStatus').textContent = '';
  $('#composerStatus').setAttribute('aria-hidden', 'true');
  $('#emptyState').hidden = true;
  $('#messages').append(optimisticMessage, typingElement());
  input.value = '';
  clearImageSelection();
  scheduleLatestScroll(true);
  let errorMessage = null;
  try {
    let attachmentIds = [];
    if (submittedFiles.length) {
      const form = new FormData();
      for (const file of submittedFiles) form.append('images', file);
      const uploaded = await request(`/api/rooms/${state.room.id}/attachments/batch`, { method: 'POST', body: form });
      attachmentIds = uploaded.attachments.map((attachment) => attachment.id);
    }
    const response = await fetch(`/api/rooms/${state.room.id}/messages/stream`, {
      method: 'POST',
      headers: { 'X-Together-CSRF': state.csrf, 'Content-Type': 'application/json' },
      body: JSON.stringify({ content, attachment_ids: attachmentIds })
    });
    if (!response.ok || !response.body) throw new Error('Together could not start that request.');
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let pending = '';
    let assistant = null;
    while (true) {
      const { value, done } = await reader.read();
      pending += decoder.decode(value || new Uint8Array(), { stream: !done });
      const frames = pending.split('\n\n'); pending = frames.pop();
      for (const frame of frames) {
        const event = /^event: (.+)$/m.exec(frame)?.[1];
        const data = /^data: (.+)$/m.exec(frame)?.[1];
        if (!event || !data) continue;
        const payload = JSON.parse(data);
        if (event === 'user') {
          userMessageReceived = true;
          state.seenMessageIds.add(String(payload.id));
          state.lastUserMessage = payload;
          optimisticMessage.replaceWith(messageElement(payload));
          scheduleLatestScroll();
        }
        if (event === 'delta') {
          removeTypingIndicator();
          if (!assistant) { assistant = messageElement({ role: 'assistant', author: 'Pip', content: '' }, state.lastUserMessage); $('#messages').append(assistant); }
          assistant.querySelector('.message-body').textContent += payload.content;
          scheduleLatestScroll();
        }
        if (event === 'assistant') {
          removeTypingIndicator();
          state.seenMessageIds.add(String(payload.id));
          const rendered = messageElement(payload, state.lastUserMessage);
          if (assistant) assistant.replaceWith(rendered); else $('#messages').append(rendered);
          assistant = rendered;
          scheduleLatestScroll();
        }
        if (event === 'error') throw new Error(payload.detail);
      }
      if (done) break;
    }
    $('#emptyState').hidden = true;
  } catch (err) {
    removeTypingIndicator();
    errorMessage = err.message;
    // Restore the draft only if the request failed before the server accepted it,
    // and the user has not started composing a newer message meanwhile.
    if (!userMessageReceived && !input.value && state.files.length === 0) {
      optimisticMessage.remove();
      input.value = content;
      if (submittedFiles.length) addSelectedFiles(submittedFiles, false);
    } else if (!userMessageReceived) {
      optimisticMessage.remove();
    }
  } finally {
    for (const url of optimisticImageUrls) URL.revokeObjectURL(url);
    button.disabled = false;
    state.sendingMessage = false;
    if (state.room) selectRoom(state.room); // re-sync list + status, drop any partial bubble
    if (errorMessage) $('#composerStatus').textContent = errorMessage;
    input.focus();
  }
});

$('#logoutButton').addEventListener('click', async () => {
  await request('/api/auth/logout', { method: 'POST' });
  state.csrf = null;
  state.username = null;
  state.room = null;
  sessionStorage.removeItem('together-csrf');
  setLoggedIn(false);
  $('#password').value = '';
});

$('#newRoomButton').addEventListener('click', () => {
  $('#newRoomError').hidden = true;
  $('#newRoomDialog').showModal();
  $('#newRoomTitle').focus();
});

$('#cancelNewRoom').addEventListener('click', () => $('#newRoomDialog').close());

$('#newRoomForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  const error = $('#newRoomError');
  error.hidden = true;
  const title = $('#newRoomTitle').value.trim();
  if (!title) return;
  try {
    const room = await request('/api/rooms', {
      method: 'POST',
      body: JSON.stringify({ title, visibility: new FormData(event.currentTarget).get('visibility') })
    });
    $('#newRoomDialog').close();
    $('#newRoomTitle').value = '';
    selectRoom(room);
  } catch (err) {
    error.textContent = err.message;
    error.hidden = false;
  }
});

$('#manageRoomButton').addEventListener('click', () => {
  if (!state.room) return;
  $('#renameRoomTitle').value = state.room.title;
  $('#manageRoomError').hidden = true;
  $('#manageRoomDialog').showModal();
});

$('#cancelManageRoom').addEventListener('click', () => $('#manageRoomDialog').close());

$('#manageRoomForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  const error = $('#manageRoomError');
  error.hidden = true;
  try {
    await request(`/api/rooms/${state.room.id}`, {
      method: 'PATCH',
      body: JSON.stringify({ title: $('#renameRoomTitle').value.trim() })
    });
    state.room.title = $('#renameRoomTitle').value.trim();
    $('#manageRoomDialog').close();
    selectRoom(state.room);
  } catch (err) {
    error.textContent = err.message;
    error.hidden = false;
  }
});

$('#archiveRoomButton').addEventListener('click', async () => {
  if (!state.room) return;
  try {
    await request(`/api/rooms/${state.room.id}/archive`, { method: 'POST' });
    state.room = null;
    $('#messages').replaceChildren();
    $('#manageRoomDialog').close();
    $('#roomTitle').textContent = 'Choose a room';
    $('#emptyState').hidden = false;
    await loadRooms();
  } catch (err) {
    $('#manageRoomError').textContent = err.message;
    $('#manageRoomError').hidden = false;
  }
});

window.setInterval(pollRoomMessages, MESSAGE_POLL_INTERVAL_MS);
window.setInterval(refreshRoomActivity, ROOM_ACTIVITY_POLL_INTERVAL_MS);

if (state.csrf) openTogether().catch(() => { state.csrf = null; sessionStorage.removeItem('together-csrf'); });
