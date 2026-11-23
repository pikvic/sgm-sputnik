(() => {
  const el = id => document.getElementById(id);
  const fields = ["id", "object_id", "goal", "target", "place_query", "product", "start", "end", "bbox", "object_ref", "radius_km", "product_reason", "region_reason", "date_reason", "limitations", "confirmed"];
  let session, activeId, miniMap, drawFirst, thinkingTimer;
  let searches = [], goal = "", busy = false, loaded = false, saving = false, drawMode = false;
  let markers = [], mapSignature = "", locateVersion = 0;
  const markdown = window.markdownit?.({html: false, linkify: false}).disable("image");
  if (markdown) {
    const original = markdown.renderer.rules.link_open || ((tokens, i, options, env, self) => self.renderToken(tokens, i, options));
    markdown.renderer.rules.link_open = (tokens, i, options, env, self) => {
      tokens[i].attrSet("target", "_blank"); tokens[i].attrSet("rel", "noopener noreferrer");
      return original(tokens, i, options, env, self);
    };
  }
  async function api(path, body) {
    const response = await fetch(path, body === undefined ? {} : {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Не удалось выполнить запрос.");
    return data;
  }
  const event = (kind, productId) => api("/api/events", {kind, product: productId}).catch(() => {});
  const uid = () => crypto.randomUUID().replaceAll("-", "");
  const blank = objectId => ({id: uid(), object_id: objectId || uid(), target: "", product: "", start: "", end: "", object_ref: "", confirmed: false});
  const active = () => searches.find(row => row.id === activeId);
  const group = () => searches.filter(row => row.object_id === active()?.object_id);
  const clean = row => Object.fromEntries(fields.filter(key => row[key] !== undefined && row[key] !== null).map(key => [key, row[key]]));
  const context = () => ({...clean(active() || blank()), goal, searches: searches.map(row => ({...clean(row), goal}))});
  function setContext(value) {
    goal = value.goal || "";
    searches = value.searches?.length ? value.searches : [{...blank(), ...value}];
    for (const row of searches) { row.id ||= uid(); row.object_id ||= uid(); }
    activeId = searches.some(row => row.id === value.id) ? value.id : searches[0].id;
    renderResearch();
  }
  function status(text, error = false) {
    el("assistant-status").textContent = text; el("assistant-status").classList.toggle("error", error);
  }
  function buttons() {
    const blocked = busy || saving, row = active();
    const undated = row?.product && !catalog.find(p => p.id === row.product)?.start;
    const ready = Boolean(row?.product && row.bbox && (undated || row.start && row.end));
    el("assistant-send").disabled = !session?.enabled || !session?.consent || blocked;
    el("assistant-reset").disabled = blocked;
    el("assistant-scenes").disabled = !session?.enabled || !session?.consent || !ready || !row?.confirmed || blocked;
    el("research-search").disabled = !ready || !row?.confirmed || blocked;
    el("research-confirm").disabled = !ready || row?.confirmed || blocked;
    for (const id of ["task-goal", "task-target", "research-product", "research-start", "research-end", "research-radius", "research-delete", "research-delete-object", "research-add", "research-add-product", "research-from-manual", "research-draw", "research-view", "geocode-search"]) el(id).disabled = blocked;
    el("research-radius").disabled = blocked || !row?.object_ref;
    el("research-start").disabled = el("research-end").disabled = blocked || undated;
    el("geocode-reverse").disabled = blocked || !session?.external_geocoding;
    el("assistant-input").disabled = busy;
    for (const button of el("research-searches").querySelectorAll("button")) button.disabled = blocked;
  }
  async function persist() {
    if (saving || busy) return;
    saving = true; buttons();
    try {
      const result = await api("/api/assistant/context", context()); setContext(result.context); return result;
    } catch (error) { status(error.message, true); throw error; }
    finally { saving = false; buttons(); }
  }
  async function refresh(restore = false) {
    session = await api("/api/assistant/session");
    el("assistant-login").hidden = !session.login_enabled || session.authenticated && !session.admin_relogin_needed;
    el("assistant-login").textContent = session.admin_relogin_needed ? "Повторить вход через Яндекс" : "Войти через Яндекс";
    el("assistant-logout").hidden = !session.authenticated; el("assistant-admin").hidden = !session.admin;
    el("assistant-account").textContent = session.admin ? "Администратор · без квот" : session.authenticated ? "Вход выполнен · ID " + session.yandex_id : "Анонимная сессия";
    el("assistant-role-note").hidden = !session.admin_relogin_needed;
    el("assistant-role-note").textContent = "Для проверки настроенного логина администратора повторите вход через Яндекс. Числовой ID текущего аккаунта: " + session.yandex_id + ".";
    el("assistant-consent-row").hidden = session.consent; el("assistant-consent").checked = session.consent;
    el("assistant-disclosure").textContent = (session.local_model ? "Текст задачи, координаты и метаданные передаются локальной модели на сервере Ollama. " : "Текст задачи, координаты и метаданные передаются облачному ИИ. Ответственность за допустимость передачи сведений несёт пользователь. ") + "Превью и спутниковые файлы модели не отправляются. История и объекты хранятся в текущей сессии. Администратор видит техническую статистику. Рекомендации предварительны; геологические выводы делает исследователь.";
    el("assistant-consent-label").textContent = session.local_model ? "Я понимаю условия передачи данных локальному ИИ" : "Я понимаю условия передачи данных в облако";
    el("assistant-budget").textContent = session.admin ? "Квоты сняты. Использование модели учитывается в телеметрии." : "Лимит сессии: " + session.remaining_tokens.toLocaleString("ru-RU") + " токенов · " + session.remaining_requests + " запросов. Общий остаток: " + session.global_remaining_tokens.toLocaleString("ru-RU") + ".";
    if (!session.enabled) status("Модель пока не подключена администратором.");
    if (restore) {
      setContext(session.context || {}); el("assistant-messages").replaceChildren();
      for (const row of session.messages) {
        if (row.role === "assistant" && row.answer) showAnswer(row.answer, session.context, true);
        else message(row.role, row.content);
      }
      el("assistant-welcome").hidden = session.messages.length > 0;
    }
    buttons();
  }
  function message(role, text) {
    el("assistant-welcome").hidden = true;
    const row = node("article", "", "assistant-message"); row.dataset.role = role;
    row.append(node("p", role === "user" ? "Вы" : "Помощник", "message-role"));
    const body = node("div", "", "markdown-body");
    if (markdown) body.innerHTML = markdown.render(text); else body.textContent = text;
    row.append(body); el("assistant-messages").append(row); return row;
  }
  function reveal(row) {
    const scroller = el("assistant-scroll");
    scroller.scrollTop += row.getBoundingClientRect().top - scroller.getBoundingClientRect().top - 14;
  }
  function sourceLink(url, title) {
    const link = node("a", title); link.href = url; link.target = "_blank"; link.rel = "noopener noreferrer"; return link;
  }
  function showCandidates(items, container, destinationObject) {
    for (const item of items) {
      const candidate = node("div", "", "place-candidate");
      candidate.append(node("strong", item.name), node("p", [item.type, item.region].filter(Boolean).join(" · "), "muted"));
      candidate.append(node("p", item.coordinate_note || "Проверьте положение и область поиска.", "muted"));
      if (item.source_url) candidate.append(sourceLink(item.source_url, item.source_name || "Источник координат"));
      const choose = node("button", "Выбрать этот объект"); choose.type = "button";
      choose.addEventListener("click", async () => {
        if (busy || saving) return;
        const rows = searches.filter(row => row.object_id === destinationObject);
        if (!rows.length) { status("Эта карточка объекта уже удалена.", true); return; }
        activeId = rows[0].id;
        for (const row of rows) {
          row.object_ref = item.id; row.target = item.name; row.object = item; row.confirmed = false;
          row.radius_km ||= 20;
          row.region_reason ||= "Предварительно предложен обзор окрестностей в радиусе 20 км. Проверьте масштаб под свою задачу.";
          delete row.bbox; delete row.geometry;
        }
        try { await persist(); event("object_confirm"); } catch {}
      });
      candidate.append(choose); container.append(candidate);
    }
  }
  function showAnswer(result, snapshot, restored = false) {
    const row = message("assistant", result.reply);
    if (result.place_candidates?.length && !active()?.object_ref) {
      const candidates = node("details", "", "message-candidates"); candidates.open = true;
      candidates.append(node("summary", "Найденные геологические объекты · выберите подходящий"));
      showCandidates(result.place_candidates, candidates, result.object_id || active()?.object_id); row.append(candidates);
    }
    const query = result.task?.place_query;
    if (query) {
      const locate = node("button", "Уточнить положение: " + query); locate.type = "button";
      locate.addEventListener("click", () => { el("geocode-query").value = query; el("geocode-details").open = true; geocode("search"); }); row.append(locate);
    }
    if (result.plan?.length) row.append(node("p", "Продукты, области и периоды предложены в панели объектов справа. Проверьте параметры каждого поиска.", "muted"));
    if (result.evidence_items || result.scene_recommendations?.length) row.append(node("p", "Проверено записей: " + result.searched_count + (result.has_more_scenes ? ". В каталоге есть следующие страницы." : "."), "muted"));
    for (const recommendation of result.scene_recommendations || []) {
      const suggestion = node("div", "", "assistant-suggestion"); suggestion.append(node("p", recommendation.reason));
      const coverage = recommendation.overlap_percent == null ? "Пересечение не определено" : "Геометрический охват области " + recommendation.overlap_percent + "%";
      suggestion.append(node("p", [sceneDate(recommendation.date), coverage, cloudLabel(recommendation.cloud)].filter(Boolean).join(" · "), "muted"));
      suggestion.append(node("p", "Облачность относится ко всей сцене. Видимость поверхности над объектом не проверялась.", "muted"));
      if (recommendation.limitations) suggestion.append(node("p", recommendation.limitations, "muted"));
      if (result.evidence_items && snapshot?.bbox) {
        const open = node("button", "Открыть сцену"); open.type = "button";
        open.addEventListener("click", () => openEvidence(result, snapshot, recommendation.id)); suggestion.append(open);
      } else if (restored) suggestion.append(node("p", "Для актуальных файлов повторите поиск по карточке продукта.", "muted"));
      row.append(suggestion);
    }
    if (result.sources?.length) {
      const sources = node("details"); sources.append(node("summary", "Источники справочной базы"));
      const links = node("div", "", "assistant-sources");
      for (const source of result.sources) links.append(sourceLink(source.url, source.title));
      sources.append(links); row.append(sources);
    }
    if (!restored) reveal(row);
  }
  function thinking(enabled, mode) {
    el("assistant-thinking").hidden = !enabled; el("assistant-messages").setAttribute("aria-busy", String(enabled));
    clearInterval(thinkingTimer); if (!enabled) return;
    const started = Date.now();
    el("assistant-thinking-detail").textContent = mode === "scenes" ? "Получает сцены STAC и сравнивает пересечения и метаданные…" : "Уточняет контекст и проверяет справочную базу…";
    el("assistant-elapsed").textContent = "0 с";
    thinkingTimer = setInterval(() => {
      const elapsed = Math.floor((Date.now() - started) / 1000); el("assistant-elapsed").textContent = elapsed + " с";
      if (elapsed >= 20) el("assistant-thinking-detail").textContent = "Ответ ещё формируется. Первый запрос к локальной модели может занять больше времени.";
    }, 1000);
    reveal(el("assistant-thinking"));
  }
  async function send(mode) {
    if (busy || saving || !session?.consent || !session?.enabled) return;
    const text = mode === "scenes" ? "Подбери конкретные сцены для выбранного подтверждённого поиска. Раздельно объясни геометрический охват и ограничения метаданных." : el("assistant-input").value.trim();
    if (!text) return;
    if (mode === "scenes" && !active()?.confirmed) { status("Подтвердите параметры выбранного продукта.", true); return; }
    busy = true; buttons(); status(""); const snapshot = structuredClone(active());
    reveal(message("user", text)); thinking(true, mode);
    try {
      const result = await api("/api/assistant/chat", {message: text, mode, context: context()});
      setContext(result.context); showAnswer(result, snapshot);
      if (mode !== "scenes") el("assistant-input").value = "";
      await refresh();
      const query = result.task?.place_query || active()?.place_query;
      if (query && !active()?.object_ref && !result.place_candidates?.length) { el("geocode-query").value = query; el("geocode-details").open = true; await geocode("search"); }
    } catch (error) { status(error.message, true); }
    finally { thinking(false); busy = false; buttons(); el("assistant-input").focus(); }
  }
  function description(label, value, reason) {
    const section = node("div", "", "context-fact"); section.append(node("span", label, "context-label"), node("strong", value));
    if (reason) section.append(node("p", reason, "muted")); return section;
  }
  function renderResearch() {
    const row = active(); if (!row) return;
    const list = el("research-searches"); list.replaceChildren();
    const objects = new Map();
    for (const search of searches) { if (!objects.has(search.object_id)) objects.set(search.object_id, []); objects.get(search.object_id).push(search); }
    for (const [objectId, queries] of objects) {
      const section = node("section", "", "research-object"); section.dataset.objectId = objectId;
      section.append(node("h3", queries[0].target || "Новый объект"));
      const products = node("div", "", "object-products");
      for (const query of queries) {
        const button = node("button", "", "research-card"); button.type = "button"; button.setAttribute("aria-pressed", String(query.id === activeId));
        button.append(node("strong", catalog.find(p => p.id === query.product)?.name || "Продукт ещё не выбран"));
        const period = query.start && query.end ? query.start + " — " + query.end : query.product === "copernicus-dem" ? "Без периода" : "Период уточняется";
        button.title = period + (query.radius_km ? " · радиус " + query.radius_km + " км" : "") + (query.confirmed ? " · подтверждено" : " · предложение");
        button.append(node("span", period + (query.radius_km ? " · R " + query.radius_km + " км" : ""), "muted"), node("span", query.confirmed ? "Подтверждено" : "Предложение", "search-state"));
        button.addEventListener("click", () => { if (!busy && !saving) { activeId = query.id; renderResearch(); persist().catch(() => {}); } }); products.append(button);
      }
      section.append(products); list.append(section);
    }
    el("task-goal").value = goal; el("task-target").value = row.target || "";
    const select = el("research-product");
    if (select.options.length !== catalog.length + 1) { select.replaceChildren(new Option("Пока не выбран", "")); for (const item of catalog) select.append(new Option(item.name, item.id)); }
    select.value = row.product || ""; el("research-start").value = row.start || ""; el("research-end").value = row.end || ""; el("research-radius").value = row.radius_km || "";
    const undated = row.product && !catalog.find(p => p.id === row.product)?.start;
    const details = el("research-context-details"); details.replaceChildren();
    details.append(description("Данные", catalog.find(p => p.id === row.product)?.name || "Помощник предложит продукт", row.product_reason));
    details.append(description("Область этого поиска", row.radius_km ? "Радиус " + row.radius_km + " км от точки объекта" : row.bbox ? "Область задана на карте" : "Положение объекта уточняется", row.region_reason || (row.bbox ? "Границы выбраны пользователем." : "Найдите объект по названию или выделите область на карте.")));
    if (row.object) {
      const provenance = node("details", "", "object-provenance"); provenance.append(node("summary", "Координаты и источник"));
      provenance.append(node("p", row.object.point.map(v => Number(v.toFixed(5))).join(", ") + " · WGS 84", "muted"), node("p", row.object.coordinate_note, "muted"));
      provenance.append(sourceLink(row.object.source_url, row.object.source_name || "Источник")); details.append(provenance);
    }
    details.append(description("Период этого поиска", undated ? "Для модели рельефа не требуется" : row.start && row.end ? row.start + " — " + row.end : "Пока не выбран", row.date_reason));
    if (row.limitations) details.append(description("Ограничения", row.limitations));
    el("research-note").textContent = row.confirmed ? "Параметры подтверждены. Можно выполнить поиск." : "Область и период этого продукта нужно подтвердить.";
    renderMap(); buttons();
  }
  function edit(change) { if (busy || saving) return; Object.assign(active(), change, {confirmed: false}); persist().catch(() => {}); }
  async function geocode(mode) {
    const version = ++locateVersion, destination = active()?.object_id, params = {mode};
    if (mode === "reverse") { if (!miniMap) return; const center = miniMap.getCenter().wrap(); Object.assign(params, {lat: center.lat, lon: center.lng}); }
    else params.q = el("geocode-query").value.trim();
    if (mode === "search" && !params.q) return;
    el("geocode-status").textContent = "Ищем в справочнике и географических источниках…"; el("geocode-results").replaceChildren();
    try {
      const data = await api("/api/geocode?" + new URLSearchParams(params));
      if (version !== locateVersion || destination !== active()?.object_id) return;
      el("geocode-status").textContent = (data.warning ? data.warning + " " : "") + (data.items.length ? "Выберите подходящий объект. " : "Объект пока не найден. Попробуйте синоним или выделите участок на карте. ") + data.attribution;
      showCandidates(data.items, el("geocode-results"), destination);
    } catch (error) { if (version === locateVersion) el("geocode-status").textContent = error.message; }
  }
  function planFields(row) {
    chooseProduct(catalog.find(p => p.id === row.product)); setArea(row.bbox, "custom", true);
    dateFields[0].value = row.start || ""; dateFields[1].value = row.end || ""; clearResults();
    researchAOI = row.geometry || null;
    if (researchAOI) setGeometry("search-area", researchAOI);
  }
  function openEvidence(result, snapshot, id) {
    setMode("manual"); planFields(snapshot);
    searchParams = {product: snapshot.product, bbox: snapshot.bbox.join(","), start: snapshot.start, end: snapshot.end};
    if (snapshot.geometry) searchParams.region = JSON.stringify(snapshot.geometry);
    appendResults(result.evidence_items); nextPage = result.next_page; el("more").hidden = !nextPage; el("results-body").hidden = false;
    el("result-status").textContent = "Показано записей: " + result.evidence_items.length;
    const index = result.evidence_items.findIndex(item => item.id === id);
    if (index >= 0) chooseScene(result.evidence_items[index], el("items").children[index]); event("scene_open", snapshot.product);
  }
  function mapNote(text) { el("research-map-note").textContent = text; el("research-map-note").hidden = !text; }
  function setupMiniMap() {
    if (miniMap || !window.maplibregl) return;
    try {
      miniMap = new maplibregl.Map({container: "research-map", center: [97, 57], zoom: 2, maxPitch: 0, dragRotate: false, touchPitch: false,
        locale: {"NavigationControl.ZoomIn": "Приблизить", "NavigationControl.ZoomOut": "Отдалить", "AttributionControl.ToggleAttribution": "Источники карты", "Map.Title": "Карта исследования"},
        style: {version: 8, sources: {osm: {type: "raster", tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"], tileSize: 256, maxzoom: 19, attribution: '© <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a>'}}, layers: [{id: "osm", type: "raster", source: "osm"}]}});
      miniMap.addControl(new maplibregl.NavigationControl({showCompass: false}), "top-right");
      miniMap.addControl(new maplibregl.ScaleControl({unit: "metric"}), "bottom-left");
      miniMap.on("load", () => {
        miniMap.addSource("regions", {type: "geojson", data: {type: "FeatureCollection", features: []}});
        miniMap.addLayer({id: "regions-fill", type: "fill", source: "regions", paint: {"fill-color": ["get", "color"], "fill-opacity": .12}});
        miniMap.addLayer({id: "regions-line", type: "line", source: "regions", paint: {"line-color": ["get", "color"], "line-width": 2, "line-dasharray": [3, 2]}}); renderMap(true);
      });
      miniMap.on("click", e => {
        if (!drawMode || busy || saving) return;
        if (!drawFirst) { drawFirst = e.lngLat; mapNote("Укажите противоположный угол области."); return; }
        const bbox = corners(drawFirst, e.lngLat);
        if (bbox[0] === bbox[2] || bbox[1] === bbox[3]) { mapNote("Область должна иметь площадь. Укажите другой угол."); return; }
        stopDraw(); delete active().radius_km; edit({bbox, region_reason: "Прямоугольная область выделена пользователем на карте."});
      });
    } catch { mapNote("Карта недоступна. Можно взять область из ручного поиска."); }
  }
  function stopDraw() {
    drawMode = false; drawFirst = null; el("research-draw").setAttribute("aria-pressed", "false");
    if (miniMap) miniMap.getCanvas().style.cursor = ""; mapNote("");
  }
  function renderMap(force = false) {
    if (!miniMap?.getSource("regions")) return;
    const signature = JSON.stringify(searches.map(row => [row.id, row.bbox, row.radius_km, row.object_ref, row.target])) + activeId;
    if (signature === mapSignature && !force) return;
    mapSignature = signature; for (const marker of markers) marker.remove(); markers = [];
    const features = [], seen = new Set();
    for (const row of [...searches].sort((a, b) => Number(b.id === activeId) - Number(a.id === activeId))) {
      const color = row.id === activeId ? "#315ab8" : "#7588a8";
      if (row.geometry) features.push({type: "Feature", geometry: row.geometry, properties: {color}});
      else if (row.bbox) features.push({type: "Feature", geometry: boxGeometry(row.bbox), properties: {color}});
      if (row.id === activeId && row.bbox) {
        const [west, , east, north] = row.bbox;
        const label = node("div", "Поиск: " + (catalog.find(p => p.id === row.product)?.name || "область") + (row.radius_km ? " · " + row.radius_km + " км" : ""), "region-label");
        markers.push(new maplibregl.Marker({element: label, anchor: "bottom"}).setLngLat([(west + (east < west ? east + 360 : east)) / 2, north]).addTo(miniMap));
      }
      if (seen.has(row.object_id)) continue; seen.add(row.object_id);
      const point = row.object?.point || (row.bbox ? [(row.bbox[0] + (row.bbox[2] < row.bbox[0] ? row.bbox[2] + 360 : row.bbox[2])) / 2, (row.bbox[1] + row.bbox[3]) / 2] : null);
      if (!point) continue;
      const markerEl = node("div", "", "research-marker" + (row.object ? " object-marker" : " region-marker"));
      markerEl.append(node("span", "", "marker-dot"), node("span", row.object?.name || row.target || "Область поиска", "marker-label"));
      markers.push(new maplibregl.Marker({element: markerEl, anchor: "bottom-left"}).setLngLat(point).addTo(miniMap));
    }
    miniMap.getSource("regions").setData({type: "FeatureCollection", features}); fitMap(false);
  }
  function fitMap(all = true) {
    if (!miniMap) return;
    const rows = all ? searches : [active()].filter(Boolean), bounds = new maplibregl.LngLatBounds();
    for (const row of rows) {
      if (row.bbox) { const [w, s, e, n] = row.bbox; bounds.extend([w, s]); bounds.extend([e < w ? e + 360 : e, n]); }
      else if (row.object) bounds.extend(row.object.point);
    }
    if (!bounds.isEmpty()) miniMap.fitBounds(bounds, {padding: 35, maxZoom: 12, duration: 500});
  }
  async function setMode(mode) {
    const ai = mode === "assistant";
    el("manual-workspace").hidden = ai; el("assistant-workspace").hidden = !ai;
    el("manual-mode").setAttribute("aria-pressed", String(!ai)); el("assistant-mode").setAttribute("aria-pressed", String(ai));
    document.body.classList.toggle("assistant-mode", ai);
    history.replaceState(null, "", location.pathname + location.search + (ai ? "#assistant" : "#manual"));
    if (!ai) { requestAnimationFrame(() => map?.resize()); return; }
    setupMiniMap(); requestAnimationFrame(() => miniMap?.resize());
    try { await refresh(!loaded); loaded = true; event("assistant_open"); } catch (error) { status(error.message, true); }
  }
  function table(rows) {
    const element = node("table", "", "admin-table");
    for (const [label, value] of rows) { const row = node("tr"); row.append(node("th", label), node("td", String(value))); element.append(row); } return element;
  }
  el("manual-mode").addEventListener("click", () => setMode("manual"));
  el("assistant-mode").addEventListener("click", () => setMode("assistant"));
  window.addEventListener("hashchange", () => setMode(location.hash === "#assistant" ? "assistant" : "manual"));
  window.addEventListener("sputnik:catalog-ready", () => { if (loaded) renderResearch(); });
  el("assistant-form").addEventListener("submit", e => { e.preventDefault(); send("discuss"); });
  el("assistant-input").addEventListener("keydown", e => { if (e.ctrlKey && e.key === "Enter" && !e.isComposing) { e.preventDefault(); if (!el("assistant-send").disabled) el("assistant-form").requestSubmit(); } });
  for (const button of document.querySelectorAll("[data-prompt]")) button.addEventListener("click", () => { el("assistant-input").value = button.dataset.prompt; el("assistant-input").focus(); });
  el("assistant-consent").addEventListener("change", async () => {
    if (!el("assistant-consent").checked) return;
    try { await api("/api/assistant/consent", {accepted: true}); await refresh(); } catch (error) { status(error.message, true); }
  });
  el("assistant-scenes").addEventListener("click", () => send("scenes"));
  el("assistant-reset").addEventListener("click", async () => {
    if (busy || saving) return;
    try { await api("/api/assistant/reset", {}); await refresh(true); el("geocode-results").replaceChildren(); el("geocode-status").textContent = ""; status(""); } catch (error) { status(error.message, true); }
  });
  el("assistant-logout").addEventListener("click", async () => { try { await api("/auth/logout", {}); await refresh(); } catch (error) { status(error.message, true); } });
  el("assistant-admin").addEventListener("click", async () => {
    try {
      const data = await api("/api/admin/telemetry"), m = data.assistant;
      openCard("Телеметрия", "За " + data.days + " дней", [table([["Запросы ИИ", m.requests], ["Использование модели / резерв, токенов", m.charged_tokens], ["Входящие токены по usage", m.prompt_tokens], ["Исходящие токены по usage", m.completion_tokens], ["Ошибки", m.errors], ["Запросы без usage, учтён резерв", m.reserved_without_usage]]), node("p", "Учитываются все обращения к модели. Запросы администратора не расходуют квоты.", "muted"), node("h3", "Действия в сервисе"), table(data.events.map(row => [row.kind, row.count])), table(data.service.map(row => [row.path + (row.product ? " · " + row.product : ""), row.requests + " запросов · " + row.errors + " ошибок"])), node("h3", "Последние запросы"), table(data.recent.map(row => [row.created, (row.is_admin ? "Администратор · " : "Пользователь · ") + row.status + " · " + row.charged + " токенов · " + row.elapsed_ms + " мс"]))], "/api/admin/telemetry", "Открыть данные статистики");
    } catch (error) { status(error.message, true); }
  });
  el("task-goal").addEventListener("change", () => { goal = el("task-goal").value; for (const row of searches) row.confirmed = false; persist().catch(() => {}); });
  el("task-target").addEventListener("change", () => {
    const value = el("task-target").value;
    if (value !== active()?.target) {
      for (const row of group()) { for (const key of ["object", "radius_km", "bbox", "geometry"]) delete row[key]; Object.assign(row, {target: value, object_ref: "", place_query: value, confirmed: false}); }
      persist().catch(() => {});
    }
  });
  el("research-product").addEventListener("change", () => edit({product: el("research-product").value, product_reason: "Продукт выбран пользователем."}));
  for (const key of ["start", "end"]) el("research-" + key).addEventListener("change", () => edit({[key]: el("research-" + key).value, date_reason: "Период задан пользователем."}));
  el("research-radius").addEventListener("change", () => {
    const radius = Number(el("research-radius").value);
    if (!Number.isFinite(radius) || radius < .1 || radius > 500) { status("Радиус должен быть от 0,1 до 500 км.", true); renderResearch(); return; }
    edit({radius_km: radius, region_reason: "Радиус " + radius + " км вокруг точки объекта задан пользователем."});
  });
  el("research-confirm").addEventListener("click", () => { active().confirmed = true; persist().catch(() => {}); });
  el("research-add").addEventListener("click", () => {
    if (new Set(searches.map(row => row.object_id)).size >= 8 || searches.length >= 24) { status("Достигнут предел карточек текущей сессии.", true); return; }
    const row = blank(); searches.push(row); activeId = row.id; renderResearch(); persist().catch(() => {});
  });
  el("research-add-product").addEventListener("click", () => {
    if (searches.length >= 24) { status("В сессии уже 24 поиска данных.", true); return; }
    const current = active(), row = {...blank(current.object_id), target: current.target, object_ref: current.object_ref, object: current.object};
    searches.push(row); activeId = row.id; renderResearch(); el("research-parameters").open = true; persist().catch(() => {});
  });
  function remove(object) {
    const objectId = active()?.object_id;
    searches = searches.filter(row => object ? row.object_id !== objectId : row.id !== activeId);
    if (!searches.length) searches = [blank()];
    activeId = searches[0].id; renderResearch(); persist().catch(() => {});
  }
  el("research-delete").addEventListener("click", () => remove(false));
  el("research-delete-object").addEventListener("click", () => remove(true));
  el("research-from-manual").addEventListener("click", () => {
    const bbox = currentArea(); if (!bbox) { status("Сначала выберите область в ручном режиме.", true); return; }
    delete active().radius_km; edit({bbox, region_reason: "Область перенесена пользователем из ручного поиска."});
  });
  el("research-search").addEventListener("click", async () => {
    const row = active(); if (!row?.confirmed || busy || saving) return;
    planFields(row); await setMode("manual"); el("search-form").requestSubmit(); event("plan_apply", row.product);
  });
  el("geocode-search").addEventListener("click", () => geocode("search"));
  el("geocode-query").addEventListener("keydown", e => { if (e.key === "Enter") { e.preventDefault(); geocode("search"); } });
  el("geocode-reverse").addEventListener("click", () => geocode("reverse"));
  el("research-map-fit").addEventListener("click", () => fitMap());
  el("research-draw").addEventListener("click", () => {
    if (!miniMap) return; if (drawMode) { stopDraw(); return; }
    drawMode = true; drawFirst = null; el("research-draw").setAttribute("aria-pressed", "true"); miniMap.getCanvas().style.cursor = "crosshair"; mapNote("Укажите два противоположных угла. Esc — отмена.");
  });
  el("research-view").addEventListener("click", () => {
    if (!miniMap) return;
    const bounds = miniMap.getBounds(), normalize = v => ((v + 180) % 360 + 360) % 360 - 180;
    let w = normalize(bounds.getWest()), e = normalize(bounds.getEast());
    if (bounds.getEast() - bounds.getWest() >= 360) { w = -180; e = 180; }
    delete active().radius_km; stopDraw(); edit({bbox: [w, Math.max(-85, bounds.getSouth()), e, Math.min(85, bounds.getNorth())], region_reason: "Текущий вид карты выбран пользователем как область поиска."});
  });
  document.addEventListener("keydown", e => { if (e.key === "Escape" && drawMode) stopDraw(); });
  if (location.hash === "#assistant") setMode("assistant");
})();
