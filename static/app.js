"use strict";

const $ = id => document.getElementById(id);
const regions = {
  primorye: [130.4, 42.2, 139.2, 48.5],
  kamchatka: [155.2, 50.7, 166.7, 61.0],
  kurils: [145.4, 43.3, 156.7, 51.0],
  altai: [82.0, 48.5, 90.2, 53.0],
  caucasus: [36.4, 41.0, 49.5, 45.8],
  urals: [56.4, 55.1, 62.6, 59.5],
};
const coordFields = ["west", "south", "east", "north"].map($);
const dateFields = [$("start"), $("end")];
const shortDate = new Intl.DateTimeFormat("ru-RU", {dateStyle: "medium", timeZone: "UTC"});
const empty = {type: "FeatureCollection", features: []};
let catalog = [];
let product;
let map;
let drawStart = null;
let selectedGeometry = null;
let searchRequest;
let detailRequest;
let searchParams;
let nextPage;
let count = 0;

function node(tag, value, className) {
  const element = document.createElement(tag);
  if (value) element.textContent = value;
  if (className) element.className = className;
  return element;
}

async function request(path, params, signal) {
  const query = params ? "?" + new URLSearchParams(params) : "";
  const response = await fetch(path + query, {signal});
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || "Запрос не выполнен.");
  return payload;
}

function isoDay(date) {
  return date.toISOString().slice(0, 10);
}

function clearResults() {
  searchRequest?.abort();
  detailRequest?.abort();
  searchParams = null;
  nextPage = null;
  count = 0;
  selectedGeometry = null;
  clearMapPreview();
  setGeometry("selected-scene", null);
  $("search-error").hidden = true;
  $("items").replaceChildren();
  $("scene").replaceChildren(node("p", "Выберите запись для просмотра файлов.", "muted"));
  $("result-status").textContent = "Укажите область и выполните поиск.";
  $("results-body").hidden = true;
  $("more").hidden = true;
  $("search-button").disabled = !product;
}

function chooseProduct(item) {
  if (product === item) return;
  product = item;
  $("selected").hidden = false;
  $("product-name").textContent = item.name;
  $("product-level").textContent = item.level;
  $("product-spec").textContent = item.type + " · " + item.resolution;
  $("product-summary").textContent = item.summary;
  $("product-caveat").textContent = item.caveat;
  $("dates").hidden = !item.start;
  $("undated").hidden = Boolean(item.start);
  for (const field of dateFields) field.disabled = !item.start;
  if (item.start) {
    const today = new Date();
    const from = isoDay(new Date(today.getTime() - 29 * 86400000));
    const to = isoDay(today);
    if (!dateFields[0].value || !dateFields[1].value || dateFields[0].value > (item.end || to) || dateFields[1].value < item.start) {
      dateFields[0].value = item.end ? item.end.slice(0, 4) + "-01-01" : from;
      dateFields[1].value = item.end || to;
    }
  }
  clearResults();
  for (const button of $("products").querySelectorAll("button")) {
    button.setAttribute("aria-pressed", String(button.dataset.product === item.id));
  }
}

function renderCatalog() {
  const filter = $("tag").value;
  const visible = catalog.filter(item => !filter || item.tags.includes(filter));
  $("products").replaceChildren(...visible.map(item => {
    const button = node("button", "", "product-option");
    button.type = "button";
    button.dataset.product = item.id;
    button.setAttribute("aria-pressed", String(item === product));
    const tags = node("span", "", "tags");
    tags.append(...item.tags.map(tag => node("span", tag, "tag")));
    button.append(node("strong", item.name), node("span", item.type + " · " + item.resolution, "product-type"), tags);
    button.addEventListener("click", () => chooseProduct(item));
    return button;
  }));
  if (!visible.includes(product) && visible.length) chooseProduct(visible[0]);
}

function currentArea() {
  if (coordFields.some(field => field.value === "")) return null;
  const box = coordFields.map(field => Number(field.value));
  const [west, south, east, north] = box;
  return box.every(Number.isFinite) && west >= -180 && west <= 180 && east >= -180 && east <= 180
    && south >= -90 && north <= 90 && south < north && west !== east && !(west === 180 && east === -180)
    ? box : null;
}

function boxGeometry(box) {
  const [west, south, east, north] = box;
  const ring = (left, right) => [[left, south], [right, south], [right, north], [left, north], [left, south]];
  if (west <= east) return {type: "Polygon", coordinates: [ring(west, east)]};
  const parts = [];
  if (west < 180) parts.push([ring(west, 180)]);
  if (east > -180) parts.push([ring(-180, east)]);
  return {type: "MultiPolygon", coordinates: parts};
}

function setGeometry(name, geometry) {
  const source = map?.getSource(name);
  if (!source) return;
  source.setData(geometry ? {type: "Feature", geometry, properties: {}} : empty);
  if (name === "search-area") $("legend-area").hidden = !geometry;
  if (name === "selected-scene") $("legend-scene").hidden = !geometry;
  $("map-legend").hidden = $("legend-area").hidden && $("legend-scene").hidden;
}

function focusArea(box) {
  if (!map || !box || box.length !== 4) return;
  const [west, south, east, north] = box;
  map.fitBounds([[west, Math.max(-85, south)], [east < west ? east + 360 : east, Math.min(85, north)]], {
    padding: 36, maxZoom: 13, duration: 0,
  });
}

function clearMapPreview() {
  if (map?.getLayer("scene-preview")) map.removeLayer("scene-preview");
  if (map?.getSource("scene-preview")) map.removeSource("scene-preview");
}

function showSceneOnMap(item, tilejson) {
  if (!map.getLayer("search-area-fill")) {
    mapNote("Карта ещё загружается. Повторите действие чуть позже.");
    return;
  }
  toggleDraw(false);
  clearMapPreview();
  if (tilejson) {
    map.addSource("scene-preview", {
      type: "raster",
      url: tilejson,
      tileSize: 256,
      attribution: "Microsoft Planetary Computer",
    });
    map.addLayer({
      id: "scene-preview", type: "raster", source: "scene-preview",
      paint: {"raster-fade-duration": 0},
    }, "search-area-fill");
  }
  setGeometry("selected-scene", item.geometry);
  focusArea(item.bbox);
  $("map").scrollIntoView({block: "center"});
}

function setArea(box, region, focus) {
  coordFields.forEach((field, index) => { field.value = box ? Number(box[index].toFixed(6)) : ""; });
  $("region").value = region;
  setGeometry("search-area", box ? boxGeometry(box) : null);
  if (focus) focusArea(box);
  clearResults();
}

function mapNote(message) {
  $("map-note").textContent = message;
  $("map-note").hidden = !message;
}

function toggleDraw(active) {
  $("draw").setAttribute("aria-pressed", String(active));
  drawStart = null;
  if (map) map.getCanvas().style.cursor = active ? "crosshair" : "";
  setGeometry("draft-area", null);
  mapNote(active ? "Щёлкните в двух противоположных углах области. Esc — отмена." : "");
}

function corners(first, second) {
  const a = first.wrap();
  const b = second.wrap();
  let [west, east] = [Math.min(a.lng, b.lng), Math.max(a.lng, b.lng)];
  if (east - west > 180) [west, east] = [east, west];
  return [west, Math.min(a.lat, b.lat), east, Math.max(a.lat, b.lat)];
}

function setupMap() {
  if (!window.maplibregl) {
    mapNote("Библиотека карты недоступна. Можно выбрать регион или задать координаты.");
    return;
  }
  try {
    map = new maplibregl.Map({
      container: "map",
      center: [97, 57],
      zoom: 2.3,
      maxPitch: 0,
      dragRotate: false,
      touchPitch: false,
      locale: {
        "NavigationControl.ZoomIn": "Приблизить",
        "NavigationControl.ZoomOut": "Отдалить",
        "AttributionControl.ToggleAttribution": "Источники карты",
        "Map.Title": "Карта",
      },
      style: {
        version: 8,
        sources: {
          osm: {
            type: "raster",
            tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"],
            tileSize: 256,
            maxzoom: 19,
            attribution: '© <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap contributors</a>',
          },
        },
        layers: [
          {id: "map-background", type: "background", paint: {"background-color": "#e7eef8"}},
          {id: "osm", type: "raster", source: "osm"},
        ],
      },
    });
  } catch {
    mapNote("Карта недоступна в этом браузере. Можно выбрать регион или задать координаты.");
    return;
  }
  map.addControl(new maplibregl.NavigationControl({showCompass: false}), "top-right");
  map.addControl(new maplibregl.ScaleControl({unit: "metric"}), "bottom-left");
  map.on("load", () => {
    const overlays = [
      ["search-area", "#315ab8", 0.13],
      ["draft-area", "#315ab8", 0.23],
      ["selected-scene", "#a54a17", 0.15],
    ];
    for (const [id, color, opacity] of overlays) {
      map.addSource(id, {type: "geojson", data: empty});
      map.addLayer({id: id + "-fill", type: "fill", source: id, paint: {"fill-color": color, "fill-opacity": opacity}});
      map.addLayer({id: id + "-line", type: "line", source: id, paint: {"line-color": color, "line-width": 2}});
    }
    const area = currentArea();
    setGeometry("search-area", area ? boxGeometry(area) : null);
    setGeometry("selected-scene", selectedGeometry);
    $("map-toolbar").hidden = false;
  });
  map.on("error", event => {
    if ($("draw").getAttribute("aria-pressed") === "true") return;
    if (event.sourceId === "scene-preview") {
      mapNote("Предпросмотр на карте не загрузился. Охват сцены доступен.");
    } else if (event.sourceId === "osm") {
      mapNote("Часть подложки OpenStreetMap недоступна.");
    }
  });
  map.on("mousemove", event => {
    if (drawStart) setGeometry("draft-area", boxGeometry(corners(drawStart, event.lngLat)));
  });
  map.on("click", event => {
    if ($("draw").getAttribute("aria-pressed") !== "true") return;
    if (!drawStart) {
      drawStart = event.lngLat;
      mapNote("Укажите противоположный угол.");
      return;
    }
    const area = corners(drawStart, event.lngLat);
    if (Math.min(Math.abs(area[0] - area[2]), 360 - Math.abs(area[0] - area[2])) < 0.000001 || area[3] - area[1] < 0.000001) {
      mapNote("Выберите угол чуть дальше от первого.");
      return;
    }
    toggleDraw(false);
    setArea(area, "custom", false);
  });
}

function sceneDate(value) {
  if (!value) return "";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "" : shortDate.format(date);
}

function cloudLabel(value) {
  return Number.isFinite(value) ? "облачность " + Math.round(value) + "%" : "";
}

function openCard(title, subtitle, body, url, linkText) {
  $("info-title").textContent = title;
  $("info-subtitle").textContent = subtitle;
  $("info-body").replaceChildren(...body);
  $("info-source").href = url;
  $("info-source").textContent = linkText + " ↗";
  $("info-card").showModal();
  $("info-body").scrollTop = 0;
}

const metadataFields = {
  id: ["Идентификатор записи", "Уникальное обозначение сцены или плитки у поставщика."],
  collection: ["Коллекция STAC", "Идентификатор продукта в каталоге."],
  datetime: ["Дата и время съёмки", "Момент наблюдения в формате поставщика."],
  start_datetime: ["Начало интервала", "Начало съёмки или периода наблюдений."],
  end_datetime: ["Конец интервала", "Конец съёмки или периода наблюдений."],
  platform: ["Спутник", "Обозначение космического аппарата."],
  instruments: ["Приборы", "Обозначения сенсоров, которыми получены данные."],
  "eo:cloud_cover": ["Облачность, %", "Оценка для всей сцены. На выбранном участке облачность может отличаться."],
  gsd: ["Шаг съёмки, м", "Расстояние между соседними отсчётами на поверхности по метаданным."],
  bbox: ["Границы охвата", "Запад, юг, восток, север в долготе и широте WGS 84 для двумерного охвата."],
  constellation: ["Миссия или группировка"],
  created: ["Дата создания продукта"],
  updated: ["Дата обновления записи"],
  "eo:snow_cover": ["Снежный покров, %"],
  "proj:code": ["Код системы координат"],
  "proj:epsg": ["Код EPSG"],
  "proj:shape": ["Размеры растра в пикселях"],
  "proj:transform": ["Параметры геопривязки растра"],
  "proj:bbox": ["Границы в системе координат продукта"],
  "proj:geometry": ["Контур в системе координат продукта"],
  "view:off_nadir": ["Угол отклонения от надира, °"],
  "view:sun_azimuth": ["Азимут Солнца, °"],
  "view:sun_elevation": ["Высота Солнца над горизонтом, °"],
  "sat:orbit_state": ["Направление орбиты"],
  "sat:relative_orbit": ["Номер относительной орбиты"],
  "sat:absolute_orbit": ["Номер абсолютной орбиты"],
  "sar:polarizations": ["Поляризации радарного сигнала"],
  "sar:instrument_mode": ["Режим работы радара"],
  "sar:frequency_band": ["Частотный диапазон радара"],
  "sar:product_type": ["Тип радарного продукта"],
  "s2:mgrs_tile": ["Код плитки Sentinel‑2 (MGRS)"],
  "s2:product_uri": ["Обозначение продукта Sentinel‑2"],
  "s2:processing_baseline": ["Версия обработки Sentinel‑2"],
  "landsat:scene_id": ["Идентификатор сцены Landsat"],
  "landsat:product_id": ["Идентификатор продукта Landsat"],
  "landsat:wrs_path": ["Номер маршрута Landsat (WRS)"],
  "landsat:wrs_row": ["Номер ряда Landsat (WRS)"],
  "s5p:product_name": ["Обозначение вещества или параметра"],
  "s5p:product_type": ["Код продукта TROPOMI"],
  "s5p:processing_mode": ["Режим обработки TROPOMI"],
};

function metadataList(entries) {
  const list = node("dl", "", "metadata");
  for (const [key, value] of entries) {
    const [label, explanation] = Object.hasOwn(metadataFields, key) ? metadataFields[key] : [];
    const term = node("dt", label || key);
    if (explanation) term.append(node("small", explanation));
    if (label) term.append(node("small", key));
    const definition = node("dd", typeof value === "string" ? value : JSON.stringify(value));
    const row = node("div");
    row.append(term, definition);
    list.append(row);
  }
  return list;
}

function showMetadata(details) {
  const {properties, ...fields} = details.metadata;
  const values = {...properties, ...fields};
  const mainKeys = Object.keys(metadataFields).slice(0, 10);
  const main = mainKeys.filter(key => values[key] != null).map(key => [key, values[key]]);
  const other = Object.entries(values).filter(([key]) => !mainKeys.includes(key));
  const body = [
    node("p", "Идентификаторы, даты и численные значения приведены из записи поставщика.", "muted"),
    metadataList(main),
  ];
  if (other.length) {
    const more = node("details", "", "metadata-more");
    more.append(node("summary", "Дополнительные поля STAC"), metadataList(other));
    body.push(more);
  }
  openCard("Метаданные сцены", product.name, body, details.source, "Открыть исходную запись STAC");
}

const fileNames = {
  AOT: "Оптическая толщина аэрозоля",
  B01: "Канал прибрежного аэрозоля · 60 м",
  B02: "Синий канал · 10 м", B03: "Зелёный канал · 10 м", B04: "Красный канал · 10 м",
  B05: "Красный край 1 · 20 м", B06: "Красный край 2 · 20 м", B07: "Красный край 3 · 20 м",
  B08: "Ближний ИК-канал · 10 м", B8A: "Узкий ближний ИК-канал · 20 м",
  B09: "Канал водяного пара · 60 м",
  B11: "Коротковолновый ИК 1,6 мкм · 20 м", B12: "Коротковолновый ИК 2,2 мкм · 20 м",
  SCL: "Маска классов сцены", WVP: "Содержание водяного пара",
  visual: "Изображение в естественных цветах",
  red: "Красный канал", green: "Зелёный канал", blue: "Синий канал",
  coastal: "Канал прибрежного аэрозоля", nir08: "Ближний ИК-канал",
  swir16: "Коротковолновый ИК 1,6 мкм", swir22: "Коротковолновый ИК 2,2 мкм",
  lwir: "Температура поверхности", lwir11: "Температура поверхности",
  qa: "Показатель качества температуры поверхности",
  qa_pixel: "Маска качества пикселей", qa_radsat: "Маска насыщения каналов",
  qa_aerosol: "Маска качества оценки аэрозоля", cloud_qa: "Маска облачности",
  drad: "Нисходящее тепловое излучение", urad: "Восходящее тепловое излучение",
  trad: "Измеренное тепловое излучение", emis: "Излучательная способность поверхности",
  emsd: "Стандартное отклонение излучательной способности",
  atran: "Пропускание атмосферы", cdist: "Расстояние до облаков", atmos_opacity: "Оптическая непрозрачность атмосферы",
  ang: "Коэффициенты углов съёмки", "mtl.txt": "Метаданные продукта (TXT)",
  "mtl.xml": "Метаданные продукта (XML)", "mtl.json": "Метаданные продукта (JSON)",
  VNIR: "Видимые и ближние ИК-каналы", SWIR: "Коротковолновые ИК-каналы", TIR: "Тепловые ИК-каналы",
  "qa-txt": "Отчёт контроля качества", xml: "Метаданные сцены (XML)",
  hh: "Горизонтальное излучение и приём", hv: "Горизонтальное излучение, вертикальный приём",
  vh: "Вертикальное излучение, горизонтальный приём", vv: "Вертикальное излучение и приём",
  so2: "Количество диоксида серы в столбе атмосферы",
};

function fileLabel(file, productId) {
  if (productId === "copernicus-dem" && file.key === "data") return "Высоты поверхности";
  return Object.hasOwn(fileNames, file.key) ? fileNames[file.key] : file.title;
}

function sizeLabel(bytes) {
  if (bytes == null) return "Размер недоступен";
  const units = ["Б", "КБ", "МБ", "ГБ", "ТБ"];
  const step = bytes ? Math.min(Math.floor(Math.log10(bytes) / 3), units.length - 1) : 0;
  return (bytes / 1000 ** step).toLocaleString("ru-RU", {maximumFractionDigits: 1}) + " " + units[step];
}

function describeFile(file) {
  if (file.media?.includes("geotiff") || file.media === "image/tiff") return "GeoTIFF";
  if (file.media?.includes("netcdf")) return "NetCDF";
  return "";
}

async function fillSizes(params, sizes, signal) {
  try {
    const values = await request("/api/file-sizes", params, signal);
    if (!signal.aborted) {
      for (const [key, label] of sizes) label.textContent = sizeLabel(values[key]);
    }
  } catch {
    if (!signal.aborted) {
      for (const label of sizes.values()) label.textContent = sizeLabel(null);
    }
  }
}

function renderScene(details, item, productId, signal) {
  const container = $("scene");
  container.replaceChildren(node("h3", product.name + (product.start && sceneDate(item.date) ? " · " + sceneDate(item.date) : "")));
  container.append(node("p", item.id, "muted"));
  if (details.preview) {
    const image = node("img", "", "scene-image");
    image.alt = "Предпросмотр сцены";
    image.addEventListener("error", () => image.replaceWith(node("p", "Изображение предпросмотра недоступно у поставщика.", "muted")), {once: true});
    image.src = details.preview;
    container.append(image);
  } else {
    container.append(node("p", "Поставщик не предоставил изображение для предпросмотра.", "muted"));
  }
  if (map && item.bbox?.length === 4 && item.geometry) {
    const actions = node("div", "", "scene-actions");
    const show = node("button", "Показать охват на карте");
    show.type = "button";
    show.addEventListener("click", () => showSceneOnMap(item));
    actions.append(show);
    if (details.preview && details.tilejson) {
      const overlay = node("button", "Нанести на карту", "primary");
      overlay.type = "button";
      overlay.addEventListener("click", () => showSceneOnMap(item, details.tilejson));
      actions.append(overlay);
    }
    container.append(actions);
  }
  const metadata = node("button", "Метаданные у поставщика", "link-button");
  metadata.type = "button";
  metadata.setAttribute("aria-haspopup", "dialog");
  metadata.addEventListener("click", () => showMetadata(details));
  const source = node("p");
  source.append(metadata);
  container.append(source);
  container.append(node("h3", "Файлы для скачивания"));
  if (!details.files.length) {
    container.append(node("p", "Каталог не вернул ссылок на файлы.", "muted"));
    return;
  }
  const files = node("ul", "", "file-list");
  const missingSizes = new Map();
  const params = {product: productId, id: item.id};
  for (const file of details.files) {
    const row = node("li");
    const title = fileLabel(file, productId);
    const label = node("span", title);
    const note = node("span", [file.key, describeFile(file)].filter(Boolean).join(" · ") + " · ", "file-note");
    const size = node("span", file.size == null ? "Уточняем размер…" : sizeLabel(file.size));
    if (file.size == null) missingSizes.set(file.key, size);
    note.append(size);
    label.append(note);
    const link = node("a", "Скачать");
    link.setAttribute("aria-label", "Скачать " + title);
    link.href = "/api/download?" + new URLSearchParams({...params, asset: file.key});
    link.target = "_blank";
    link.rel = "noopener";
    row.append(label, link);
    files.append(row);
  }
  container.append(files);
  if (missingSizes.size) fillSizes(params, missingSizes, signal);
}

async function chooseScene(item, selectedButton) {
  detailRequest?.abort();
  detailRequest = new AbortController();
  const {signal} = detailRequest;
  const productId = product.id;
  for (const button of $("items").querySelectorAll("button")) {
    button.setAttribute("aria-pressed", String(button === selectedButton));
  }
  selectedGeometry = item.geometry;
  clearMapPreview();
  setGeometry("selected-scene", selectedGeometry);
  $("scene").replaceChildren(node("p", "Загружаем карточку…", "muted"));
  try {
    const details = await request("/api/item", {product: productId, id: item.id}, signal);
    if (!signal.aborted) renderScene(details, item, productId, signal);
  } catch (error) {
    if (!signal.aborted) $("scene").replaceChildren(node("p", error.message, "muted"));
  }
}

function appendResults(items) {
  for (const item of items) {
    const button = node("button", "", "scene-button");
    button.type = "button";
    button.setAttribute("aria-pressed", "false");
    const date = product.start ? sceneDate(item.date) : "";
    const caption = [item.platform, cloudLabel(item.cloud)].filter(Boolean).join(" · ");
    button.append(node("strong", date || "Плитка модели"), node("span", caption || item.id));
    button.addEventListener("click", () => chooseScene(item, button));
    $("items").append(button);
  }
  count += items.length;
}

async function loadResults(token) {
  searchRequest?.abort();
  searchRequest = new AbortController();
  const {signal} = searchRequest;
  const params = {...searchParams};
  if (token) params.token = token;
  $("search-button").disabled = true;
  $("more").disabled = true;
  $("result-status").textContent = "Ищем в каталоге Planetary Computer…";
  try {
    const data = await request("/api/search", params, signal);
    if (signal.aborted) return;
    nextPage = data.next === token ? null : data.next;
    appendResults(data.items);
    $("results-body").hidden = !count && !nextPage;
    $("more").hidden = !nextPage;
    $("result-status").textContent = count ? "Показано записей: " + count
      : data.reason === "outside_archive" ? "Даты вне периода этой коллекции."
      : nextPage ? "На этой странице записей нет. Можно продолжить поиск."
      : "Данных по выбранной области и периоду не найдено.";
  } catch (error) {
    if (!signal.aborted) $("result-status").textContent = error.message;
  } finally {
    if (!signal.aborted) {
      $("search-button").disabled = false;
      $("more").disabled = false;
    }
  }
}

$("region").addEventListener("change", event => {
  toggleDraw(false);
  const region = event.target.value;
  if (region === "custom") {
    $("search-form").querySelector("details").open = true;
    return;
  }
  setArea(regions[region], region, Boolean(region));
});
for (const field of coordFields) {
  field.addEventListener("change", () => {
    toggleDraw(false);
    $("region").value = "custom";
    const area = currentArea();
    setGeometry("search-area", area ? boxGeometry(area) : null);
    focusArea(area);
    clearResults();
  });
}
for (const field of dateFields) field.addEventListener("change", clearResults);
$("draw").addEventListener("click", () => toggleDraw($("draw").getAttribute("aria-pressed") !== "true"));
document.addEventListener("keydown", event => {
  if (event.key === "Escape" && $("draw").getAttribute("aria-pressed") === "true") toggleDraw(false);
});
$("view-bounds").addEventListener("click", () => {
  toggleDraw(false);
  const bounds = map.getBounds();
  const west = bounds.getWest();
  const east = bounds.getEast();
  const wrap = value => ((value + 180) % 360 + 360) % 360 - 180;
  const range = east - west >= 360 ? [-180, 180] : [wrap(west), wrap(east)];
  setArea([range[0], bounds.getSouth(), range[1], bounds.getNorth()], "custom", false);
});
$("search-form").addEventListener("submit", async event => {
  event.preventDefault();
  clearResults();
  const bbox = currentArea();
  if (!bbox) {
    $("search-error").textContent = "Выберите регион или укажите корректные границы области.";
    $("search-error").hidden = false;
    $("region").focus();
    return;
  }
  toggleDraw(false);
  setGeometry("search-area", boxGeometry(bbox));
  focusArea(bbox);
  const [start, end] = dateFields.map(field => field.value);
  searchParams = {product: product.id, bbox: bbox.join(",")};
  if (product.start) Object.assign(searchParams, {start, end});
  await loadResults();
  if (searchParams) $("results-title").scrollIntoView({block: "nearest"});
});
$("more").addEventListener("click", () => loadResults(nextPage));
$("tag").addEventListener("change", renderCatalog);
$("product-source").addEventListener("click", () => {
  const body = [
    node("p", product.level, "muted"),
    node("p", product.description),
    node("h3", "Применение в геологии"),
    node("p", product.summary),
    node("p", product.caveat, "muted"),
  ];
  openCard(product.name, "О продукте у поставщика", body, product.source, "Открыть страницу поставщика");
});

setupMap();
request("/api/catalog").then(data => {
  catalog = data;
  const tags = [...new Set(catalog.flatMap(item => item.tags))];
  for (const tag of tags) $("tag").append(new Option(tag, tag));
  renderCatalog();
}).catch(error => {
  $("products").replaceChildren(node("p", error.message, "muted"));
});
