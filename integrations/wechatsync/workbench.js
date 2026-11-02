// The platform adapters, image upload and authenticated requests are upstream
// Wechatsync 2.0.9. This module supplies workbench fields and owned-tab readback.
import { workbenchAdapter, workbenchClient, workbenchConnect } from './assets/index.ts-Bw-475TG.js';
import config from './workbench-config.js';

const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
// MV3 requires WebSocket traffic within 30 seconds. This is a local bridge
// heartbeat only; it performs no platform requests or login renewal.
globalThis.workbenchPing = () => ({ alive: true });
chrome.alarms.create('workbench-bridge', { periodInMinutes: 1 });
chrome.alarms.onAlarm.addListener(alarm => {
  if (alarm.name === 'workbench-bridge' && !workbenchClient.isConnected()) workbenchConnect();
});
let queue = Promise.resolve();
function serial(action) {
  const next = queue.then(action, action);
  queue = next.catch(() => {});
  return next;
}

async function adapterFor(platform) {
  if (!['weixin', 'toutiao'].includes(platform)) throw new Error('Unsupported workbench platform');
  const base = await workbenchAdapter(platform);
  const adapter = new base.constructor();
  await adapter.init(Object.create(base.runtime));
  return adapter;
}

async function account(adapter, expected) {
  const auth = await adapter.checkAuth();
  if (!auth.isAuthenticated) throw new Error('平台登录已失效');
  if (expected && String(auth.userId) !== String(expected)) throw new Error('当前平台账号与稿件绑定账号不同');
  return { userId: String(auth.userId), username: auth.username, isAuthenticated: true };
}

async function ownedTab(args) {
  if (!Number.isSafeInteger(args.tabId) || !Number.isSafeInteger(args.windowId)) throw new Error('缺少本轮任务标签');
  const tab = await chrome.tabs.get(args.tabId);
  const host = args.platform === 'weixin' ? 'mp.weixin.qq.com' : 'mp.toutiao.com';
  if (tab.windowId !== args.windowId || new URL(tab.url).hostname !== host) throw new Error('任务标签归属或平台已改变');
  return tab;
}

async function readDraft(adapter, args, draftId) {
  await ownedTab(args);
  if (!/^\d+$/.test(String(draftId))) throw new Error('草稿 ID 无效');
  const url = args.platform === 'weixin'
    ? `https://mp.weixin.qq.com/cgi-bin/appmsg?t=media/appmsg_edit&action=edit&type=77&appmsgid=${draftId}&token=${adapter.weixinMeta.token}&lang=zh_CN`
    : `https://mp.toutiao.com/profile_v4/graphic/publish?pgc_id=${draftId}`;
  await chrome.tabs.update(args.tabId, { url });
  const until = Date.now() + 30000;
  while (Date.now() < until) {
    const tab = await chrome.tabs.get(args.tabId);
    if (tab.status === 'complete' && new URL(tab.url).searchParams.get(args.platform === 'weixin' ? 'appmsgid' : 'pgc_id') === String(draftId)) {
      const results = await chrome.scripting.executeScript({ target: { tabId: args.tabId }, func: platform => {
        const weixin = platform === 'weixin';
        const title = document.querySelector(weixin ? '.ProseMirror[data-placeholder="请在这里输入标题"]' : 'textarea[placeholder*="文章标题"]');
        const body = [...document.querySelectorAll('.ProseMirror[contenteditable=true]')].filter(node => !node.hasAttribute('data-placeholder'));
        const summary = document.querySelector('#js_description');
        const covers = weixin
          ? [...document.querySelectorAll('#js_cover_area .js_cover_preview_new')].filter(node => getComputedStyle(node).display !== 'none').map(node => node.style.backgroundImage.match(/url\(["']?(https:\/\/mmbiz\.qpic\.cn\/[^"')]+)["']?\)/)?.[1]).filter(Boolean)
          : [...document.querySelectorAll('.article-cover-images img')].map(node => node.currentSrc || node.src).filter(Boolean);
        const content = body.length === 1 ? body[0].cloneNode(true) : null;
        if (!weixin && content) {
          for (const placeholder of content.querySelectorAll('span[contenteditable="false"][ignoreel="true"].syl-placeholder.ProseMirror-widget')) placeholder.remove();
          for (const template of content.querySelectorAll('templ')) {
            if (template.parentElement.getAttribute('contenteditable') === 'false') template.parentElement.replaceWith(...template.childNodes);
          }
        }
        return { title: title ? (weixin ? title.innerText : title.value) : null,
          html: content ? content.innerHTML : null, summary: summary ? summary.value : null,
          covers, draftId: new URL(location.href).searchParams.get(weixin ? 'appmsgid' : 'pgc_id') };
      }, args: [args.platform] });
      const actual = results[0]?.result;
      if (actual?.title && actual.html && (args.platform !== 'weixin' || typeof actual.summary === 'string')
          && (!(args.requireCover || args.article?.cover) || actual.covers.length > 0)) {
        if (new Set(actual.covers).size > 1) throw new Error('远端草稿封面不唯一');
        return { ...actual, coverUrl: actual.covers[0] || null };
      }
    }
    await delay(300);
  }
  throw new Error('远端草稿编辑页未提供完整内容');
}

globalThis.workbenchAuth = args => serial(async () => account(await adapterFor(args.platform)));
globalThis.workbenchInspect = args => serial(async () => {
  const adapter = await adapterFor(args.platform);
  const auth = await account(adapter, args.userId);
  const actual = await readDraft(adapter, args, args.draftId);
  await account(adapter, auth.userId);
  return { auth, actual };
});

async function listDrafts(adapter, platform) {
  return adapter.withHeaderRules(adapter.HEADER_RULES, async () => {
    if (platform === 'toutiao') {
      const count = await adapter.get('https://mp.toutiao.com/mp/agw/creator_center/draft_count?type=0&app_id=1231');
      if (!Number.isInteger(count.count) || count.count < 0 || count.count > 200) throw new Error('头条草稿数量无法确认');
      const response = await adapter.get(`https://mp.toutiao.com/mp/agw/creator_center/draft_list?type=0&count=${Math.max(20, count.count)}&app_id=1231`);
      if (!Array.isArray(response.draft_list) || response.draft_list.length !== count.count) throw new Error('头条草稿列表不完整');
      return response.draft_list.map(row => ({ draftId: String(row.gid), title: row.title }));
    }
    const all = [];
    for (let begin = 0; begin < 200; begin += 20) {
      const response = await adapter.get(`https://mp.weixin.qq.com/cgi-bin/appmsg?action=list_ex&begin=${begin}&count=20&type=77&token=${adapter.weixinMeta.token}&lang=zh_CN&f=json&ajax=1`);
      if (response.base_resp?.ret !== 0 || !Array.isArray(response.app_msg_list)) throw new Error('公众号草稿列表无法确认');
      all.push(...response.app_msg_list.map(row => ({ draftId: String(row.app_id ?? row.appmsgid), title: row.multi_item?.[0]?.title || row.title, fields: Object.keys(row) })));
      if (response.app_msg_list.length < 20) return all;
    }
    throw new Error('公众号草稿列表超过核对范围');
  });
}

globalThis.workbenchFind = args => serial(async () => {
  const adapter = await adapterFor(args.platform);
  const auth = await account(adapter, args.userId);
  const drafts = await listDrafts(adapter, args.platform);
  const malformed = drafts.find(draft => !/^\d+$/.test(draft.draftId) || typeof draft.title !== 'string');
  if (malformed) throw new Error('草稿列表缺少 ID 或标题；字段：' + (malformed.fields || []).join(','));
  return { auth, total: drafts.length, drafts: drafts.filter(draft => draft.title.trim() === args.title.trim()) };
});

async function renew(args) {
  const adapter = await adapterFor(args.platform);
  const auth = await account(adapter, args.userId);
  if (args.platform === 'weixin') {
    await adapter.withHeaderRules(adapter.HEADER_RULES, async () => {
      const form = new URLSearchParams({ f: 'json', username: auth.userId });
      const response = await adapter.runtime.fetch(`https://mp.weixin.qq.com/cgi-bin/switchacct?action=switch&token=${adapter.weixinMeta.token}&lang=zh_CN`,
        { method: 'POST', credentials: 'include', headers: { 'Content-Type': 'application/x-www-form-urlencoded' }, body: form });
      const result = await response.json();
      if (result.base_resp?.ret !== 0) throw new Error('公众号同账号续期未成功，平台返回 ' + result.base_resp?.ret);
    });
    await account(adapter, auth.userId);
  }
  const storage = await chrome.storage.local.get('workbenchLogin');
  const state = storage.workbenchLogin || {};
  state[args.platform] = { userId: auth.userId, lastSuccess: Date.now(), error: null };
  await chrome.storage.local.set({ workbenchLogin: state });
  return { ...auth, renewed: args.platform === 'weixin', checkedAt: Date.now(), intervalMinutes: 360 };
}
globalThis.workbenchRenew = args => serial(() => renew(args));
chrome.alarms.create('workbench-login', { delayInMinutes: 360, periodInMinutes: 360 });
chrome.alarms.onAlarm.addListener(alarm => {
  if (alarm.name !== 'workbench-login') return;
  serial(async () => {
    const storage = await chrome.storage.local.get('workbenchLogin');
    for (const [platform, state] of Object.entries(storage.workbenchLogin || {})) {
      if (Date.now() - state.lastSuccess < 360 * 60000) continue;
      try { await renew({ platform, userId: state.userId }); }
      catch (error) {
        const current = await chrome.storage.local.get('workbenchLogin');
        current.workbenchLogin[platform] = { ...state, error: String(error.message), checkedAt: Date.now() };
        await chrome.storage.local.set({ workbenchLogin: current.workbenchLogin });
      }
    }
  });
});

globalThis.workbenchSave = args => serial(async () => {
  if (args.draftOnly !== true) throw new Error('此接入只支持保存草稿');
  await ownedTab(args);
  const adapter = await adapterFor(args.platform);
  const auth = await account(adapter, args.userId);
  const article = args.article;
  if (!article?.title || !article.html) throw new Error('稿件缺少标题或正文');
  if (args.draftId && !/^\d+$/.test(String(args.draftId))) throw new Error('已有草稿 ID 无效');
  const uploads = new Map(), imageMap = {}, uploadErrors = [];
  const fetchUpstream = adapter.runtime.fetch.bind(adapter.runtime);
  const uploadUpstream = adapter.uploadImageByUrl.bind(adapter);
  const processImagesUpstream = adapter.processImages.bind(adapter);
  let cover = null, submitted = false, savedId = null;
  adapter.processImages = async (html, upload, options) => {
    const result = await processImagesUpstream(html, upload, options);
    const styles = new Map();
    for (const match of html.matchAll(/<img\b[^>]*src="([^"]+)"[^>]*>/gi)) {
      const attributes = [...match[0].matchAll(/\s(?:style|alt|width|height)="[^"]*"/gi)].map(item => item[0]).join('');
      if (imageMap[match[1]]) styles.set(imageMap[match[1]], attributes);
    }
    return result.replace(/<img\b[^>]*src="([^"]+)"[^>]*>/gi, (tag, src) => {
      if (!styles.has(src)) return tag;
      return tag.replace(/\s(?:style|alt|width|height)="[^"]*"/gi, '').replace(/\s*\/?>(?=$)/, styles.get(src) + ' />');
    });
  };
  adapter.uploadImageByUrl = async src => {
    try {
      const result = await uploadUpstream(src);
      imageMap[src] = result.url;
      return result;
    } catch (error) { uploadErrors.push(error.message); throw error; }
  };
  const checkImages = () => {
    if (uploadErrors.length) throw new Error('正文图片上传失败：' + uploadErrors[0]);
  };
  adapter.runtime.fetch = async (url, options = {}) => {
    if (args.platform === 'toutiao' && String(url).startsWith('https://mp.toutiao.com/spice/image?')) {
      await ownedTab(args);
      const parts = [];
      for (const [name, value] of options.body.entries()) {
        if (value instanceof Blob) {
          const bytes = new Uint8Array(await value.arrayBuffer());
          let binary = '';
          for (let offset = 0; offset < bytes.length; offset += 8192) binary += String.fromCharCode(...bytes.subarray(offset, offset + 8192));
          parts.push({ name, data: btoa(binary), type: value.type, filename: value.name || 'image.jpg' });
        } else parts.push({ name, value });
      }
      const results = await chrome.scripting.executeScript({ target: { tabId: args.tabId }, world: 'MAIN',
        func: async (endpoint, parts) => {
          const csrf = await fetch('/ttwid/check/', { method: 'HEAD', credentials: 'include',
            headers: { 'x-secsdk-csrf-request': '1', 'x-secsdk-csrf-version': '1.2.22' } });
          const body = new FormData();
          for (const part of parts) {
            if (part.data) body.append(part.name, new Blob([Uint8Array.from(atob(part.data), char => char.charCodeAt(0))], { type: part.type }), part.filename);
            else body.append(part.name, part.value);
          }
          const response = await fetch(endpoint, { method: 'POST', credentials: 'include',
            headers: { 'x-secsdk-csrf-token': csrf.headers.get('x-ware-csrf-token') || '' }, body });
          return { body: await response.text(), status: response.status, type: response.headers.get('Content-Type') };
        }, args: [String(url), parts] });
      const response = results[0]?.result;
      if (!response || !response.body.trim().startsWith('{')) throw new Error('头条图片接口未返回 JSON，HTTP ' + response?.status + '，类型 ' + response?.type);
      return new Response(response.body, { status: response.status, headers: { 'Content-Type': response.type || 'application/json' } });
    }
    if (String(url).includes('/cgi-bin/operate_appmsg')) {
      checkImages();
      const form = new URLSearchParams(options.body);
      form.set('digest0', article.summary || '');
      form.set('auto_gen_digest0', '0');
      if (args.draftId) {
        form.set('AppMsgId', String(args.draftId));
        const endpoint = new URL(url);
        endpoint.searchParams.set('sub', 'update');
        url = endpoint.href;
      }
      if (cover) {
        const metadata = uploads.get(cover.url);
        const mediaId = typeof metadata?.content === 'string' ? metadata.content : metadata?.content?.media_id || metadata?.media_id;
        if (!mediaId) throw new Error('上游封面上传未返回素材 ID');
        form.set('thumb_media_id0', String(mediaId));
        for (const field of ['cdn_url0', 'cdn_235_1_url0', 'cdn_1_1_url0', 'cdn_16_9_url0', 'cdn_url_back0']) form.set(field, cover.url);
      }
      options = { ...options, body: form };
      submitted = true;
    }
    const response = await fetchUpstream(url, options);
    if (String(url).includes('/cgi-bin/filetransfer')) {
      const metadata = await response.clone().json();
      if (metadata.cdn_url) uploads.set(metadata.cdn_url, metadata);
    }
    return response;
  };
  if (args.platform === 'weixin') {
    // Production already renders the reviewed platform HTML.
    adapter.processContent = html => html;
    adapter.stripExternalLinks = html => html;
    adapter.processLatex = html => html;
  } else {
    adapter.ensureToutiaoTab = async () => { await ownedTab(args); return args.tabId; };
    const publishUpstream = adapter.publishViaContentScript.bind(adapter);
    adapter.publishViaContentScript = async (url, body) => {
      checkImages();
      const form = new URLSearchParams(body);
      // The official editor uses 2 for NONE; upstream's 3 requires ad rights.
      form.set('article_ad_type', '2');
      if (args.draftId) form.set('pgc_id', String(args.draftId));
      if (cover) {
        const attrs = cover.attrs;
        if (!attrs?.web_uri) throw new Error('上游封面上传未返回图片 URI');
        form.set('pgc_feed_covers', JSON.stringify([{ id: 0, url: cover.url, uri: attrs.web_uri,
          origin_uri: attrs.web_uri, ic_uri: '', thumb_width: Number(attrs.img_width), thumb_height: Number(attrs.img_height) }]));
        form.set('draft_form_data', JSON.stringify({ coverType: 2 }));
      }
      submitted = true;
      const response = await publishUpstream(url, form.toString());
      if (typeof response.err_no === 'number' && response.err_no !== 0) submitted = false;
      return response;
    };
  }
  try {
    if (article.cover) cover = await adapter.uploadImageByUrl(article.cover);
    const result = await adapter.publish({ ...article, markdown: '' }, { draftOnly: true });
    if (!result.success || !result.postId) throw new Error(result.error || '上游未返回草稿 ID');
    savedId = String(result.postId);
    if (args.draftId && savedId !== String(args.draftId)) throw new Error('平台返回的草稿 ID 与原稿不同');
    const actual = await readDraft(adapter, args, savedId);
    await account(adapter, auth.userId);
    return { success: true, draftId: savedId, auth, actual, coverUrl: cover?.url || null, imageMap };
  } catch (error) {
    return { success: false, submitted, draftId: savedId, auth, imageMap, coverUrl: cover?.url || null,
      error: String(error.message).replace(/([?&](?:token|ticket|key)=)[^&\s]+/g, '$1[redacted]') };
  }
});

chrome.storage.local.set({ mcpEnabled: true, mcpToken: config.token, mcpServerUrl: `ws://127.0.0.1:${config.port}` }).then(() => {
  workbenchClient.setToken(config.token);
  workbenchClient.setServerUrl(`ws://127.0.0.1:${config.port}`);
  workbenchConnect();
});
