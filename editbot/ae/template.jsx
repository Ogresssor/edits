// Сборка эдита в After Effects по описанию монтажа (DATA подставляет editbot/ae/bridge.py перед этим кодом).
// Куски фильма уже увеличены нейросетью, кадрированы и покрашены; здесь — живые слои поверх:
// движение камеры (наезд, «удар»), тряска, вспышки, размытие на переходах, субтитры, плашка, музыка.
// Все эффекты и свойства — по внутренним именам (matchName): интерфейс After Effects может быть на любом языке.

app.beginSuppressDialogs();

function P(layer, path) {            // свойство по цепочке matchName: P(L, ["ADBE Transform Group", "ADBE Scale"])
    var p = layer;
    for (var i = 0; i < path.length; i++) p = p.property(path[i]);
    return p;
}

function hexColor(h) {
    h = h.replace("#", "");
    return [parseInt(h.substr(0, 2), 16) / 255, parseInt(h.substr(2, 2), 16) / 255, parseInt(h.substr(4, 2), 16) / 255];
}

function easeAll(prop, influence) {  // плавные ключи (как Easy Ease)
    var e = new KeyframeEase(0, influence || 60);
    for (var k = 1; k <= prop.numKeys; k++) {
        var dims = prop.keyInTemporalEase(k).length;
        var arr = [];
        for (var d = 0; d < dims; d++) arr.push(e);
        try { prop.setTemporalEaseAtKey(k, arr, arr); } catch (x) {}
    }
}

function importFile(path, folder) {
    var item = app.project.importFile(new ImportOptions(new File(path)));
    if (folder) item.parentFolder = folder;
    return item;
}

function setText(layer, text, font, size, color, tracking) {
    var src = P(layer, ["ADBE Text Properties", "ADBE Text Document"]);
    var td = src.value;
    td.resetCharStyle();
    td.resetParagraphStyle();
    td.text = text;
    td.font = font;
    td.fontSize = size;
    td.applyFill = true;
    td.fillColor = color;
    td.applyStroke = false;
    td.tracking = tracking || 0;
    td.justification = ParagraphJustification.CENTER_JUSTIFY;
    src.setValue(td);
}

function fitWidth(layer, t, maxW) {  // текст шире кадра — уменьшаем слой
    var r = layer.sourceRectAtTime(t, false);
    var k = r.width > maxW ? maxW / r.width * 100 : 100;
    P(layer, ["ADBE Transform Group", "ADBE Scale"]).setValue([k, k, 100]);
    return r;
}

// ---------------------------------------------------------------- куски фильма
function addClip(comp, c, folder) {
    var item = importFile(c.file, folder);
    // частота кусков = частоте композиции, начало и конец слоя — ровно на кадр: иначе After Effects берёт то
    // предыдущий, то следующий кадр куска, и на плавном движении появляются рывки (повтор + пропуск кадра)
    try { item.mainSource.conformFrameRate = comp.frameRate; } catch (x) {}
    var fd = comp.frameDuration;
    var t0 = Math.round(c.start / fd) * fd;
    var t1 = Math.round((c.start + c.dur) / fd) * fd;
    var L = comp.layers.add(item);
    L.startTime = t0;
    L.inPoint = t0;
    L.outPoint = t1;
    // Opt-in: a trimmed MOV can have one fewer video frame than its audio duration.
    // Keep natural timing and hold only its final picture instead of rendering black.
    if (typeof c.last_frame === "number") {
        L.timeRemapEnabled = true;
        L.property("ADBE Time Remapping").expression =
            "Math.min(Math.max(time - thisLayer.startTime, 0), " + c.last_frame + ")";
        L.outPoint = t1;
    }
    L.name = c.name;
    L.motionBlur = true;
    var fx = c.fx || {};
    var extra = fx.shake ? 1.07 : 1.0;                 // запас по краям, чтобы тряска не показывала края
    var push = fx.push || 0, punch = fx.punch || 0;
    var sc = P(L, ["ADBE Transform Group", "ADBE Scale"]);
    var s0 = 100 * extra * (1 + punch);
    var s1 = 100 * extra * (1 + push * Math.min(1, 0.28 / c.dur));
    var s2 = 100 * extra * (1 + push);
    sc.setValueAtTime(t0, [s0, s0, 100]);
    if (punch > 0 && c.dur > 0.35) sc.setValueAtTime(t0 + 0.28, [s1, s1, 100]);
    sc.setValueAtTime(t1, [s2, s2, 100]);
    if (punch > 0) {                                   // «удар»: резко в начале, плавно затухает
        try { sc.setTemporalEaseAtKey(1, [new KeyframeEase(0, 10), new KeyframeEase(0, 10), new KeyframeEase(0, 10)],
                                      [new KeyframeEase(0, 85), new KeyframeEase(0, 85), new KeyframeEase(0, 85)]); } catch (x) {}
    }
    var pos = P(L, ["ADBE Transform Group", "ADBE Position"]);
    if (fx.shake) {                                     // затухающая тряска (с размытием движения)
        pos.expression = "var t=time-inPoint;var a=" + fx.shake + ";if(t>=0&&t<0.34){var k=a*(1-t/0.34);" +
                         "value+[k*Math.sin(t*95),k*0.7*Math.cos(t*77),0]}else value";
    }
    var tr = fx.transition || "";
    var w = 0.16;
    if (tr === "whip" || tr === "rgb") {                // рывок: резкий сдвиг + направленное размытие
        var dirv = (fx.dir || 1) > 0 ? 1 : -1;
        var base = [comp.width / 2, comp.height / 2, 0];       // центр кадра (не pos.value: там уже выражение)
        if (tr === "whip") {
            pos.setValueAtTime(t0, [base[0] + 110 * dirv, base[1], base[2]]);
            pos.setValueAtTime(t0 + w, base);
            easeAll(pos, 80);
        }
        var db = L.property("ADBE Effect Parade").addProperty("ADBE Motion Blur");
        db.property("ADBE Motion Blur-0001").setValue(tr === "whip" ? 90 : 0);
        var len = db.property("ADBE Motion Blur-0002");
        len.setValueAtTime(t0, 140);
        len.setValueAtTime(t0 + w, 0);
    } else if (tr === "zoomblur") {                     // радиальное «зум»-размытие
        var rb = L.property("ADBE Effect Parade").addProperty("ADBE Radial Blur");
        rb.property("ADBE Radial Blur-0003").setValue(2);
        var am = rb.property("ADBE Radial Blur-0001");
        am.setValueAtTime(t0, 45);
        am.setValueAtTime(t0 + w + 0.04, 0);
    }
    return L;
}

function addFlash(comp, t, strength) {                  // белая вспышка на сильной доле
    var s = comp.layers.addSolid([1, 1, 1], "Вспышка", comp.width, comp.height, 1, 0.2);
    s.startTime = t;
    s.inPoint = t;
    s.outPoint = t + 0.2;
    var op = P(s, ["ADBE Transform Group", "ADBE Opacity"]);
    op.setValueAtTime(t, strength);
    op.setValueAtTime(t + 0.14, 0);
    return s;
}

// ---------------------------------------------------------------- субтитры
function reveal(L, words, start, n) {                   // слова проявляются по одному (из размытия), когда звучат
    var anim = P(L, ["ADBE Text Properties", "ADBE Text Animators"]).addProperty("ADBE Text Animator");
    anim.name = "Появление слов";
    var props = anim.property("ADBE Text Animator Properties");
    props.addProperty("ADBE Text Opacity").setValue(0);
    props.addProperty("ADBE Text Blur").setValue([10, 10]);
    var sel = anim.property("ADBE Text Selectors").addProperty("ADBE Text Selector");
    try { sel.property("ADBE Text Range Advanced").property("ADBE Text Range Type2").setValue(3); } catch (x) {}
    var st = sel.property("ADBE Text Percent Start");
    st.setValueAtTime(start, 0);
    var last = start;
    for (var i = 0; i < n; i++) {
        var tw = Math.max(last + 0.01, start + words[i].t);
        st.setValueAtTime(tw, i / n * 100);
        last = Math.max(tw + 0.01, tw + 0.16);
        st.setValueAtTime(last, (i + 1) / n * 100);
    }
}

function fadeOut(L, end) {
    var op = P(L, ["ADBE Transform Group", "ADBE Opacity"]);
    op.setValueAtTime(end - 0.22, 100);
    op.setValueAtTime(end, 0);
}

function addSub(comp, ev, S) {
    var cx = comp.width / 2;
    var y1 = Math.round(comp.height * S.y_pct / 100);
    var y2 = y1 + Math.round(S.top_size * 0.62 + S.main_size * 0.72);
    var maxW = comp.width * 0.9;
    var made = [];
    if (ev.top.length) {
        var txt = [];
        for (var i = 0; i < ev.top.length; i++) txt.push(ev.top[i].text);
        var T = comp.layers.addText(txt.join(" "));
        T.name = "Субтитр (верх): " + txt.join(" ");
        setText(T, txt.join(" "), S.top_font, S.top_size, [0.96, 0.96, 0.96], S.top_tracking);
        P(T, ["ADBE Transform Group", "ADBE Position"]).setValue([cx, y1 + S.top_size * 0.35, 0]);
        T.inPoint = ev.start; T.outPoint = ev.end;
        fitWidth(T, ev.start + 0.5, maxW);
        var sh = T.property("ADBE Effect Parade").addProperty("ADBE Drop Shadow");
        sh.property("ADBE Drop Shadow-0002").setValue(160);
        sh.property("ADBE Drop Shadow-0004").setValue(2);
        sh.property("ADBE Drop Shadow-0005").setValue(10);
        reveal(T, ev.top, ev.start, ev.top.length);
        fadeOut(T, ev.end);
        made.push(T);
    }
    var mt = [];
    for (var j = 0; j < ev.main.length; j++) mt.push(ev.main[j].text);
    var M = comp.layers.addText(mt.join(" "));
    M.name = "Субтитр: " + mt.join(" ");
    setText(M, mt.join(" "), S.main_font, S.main_size, hexColor(ev.color), S.main_tracking);
    P(M, ["ADBE Transform Group", "ADBE Position"]).setValue([cx, y2 + S.main_size * 0.35, 0]);
    M.inPoint = ev.start; M.outPoint = ev.end;
    fitWidth(M, ev.start + 0.5, maxW);
    var g = M.property("ADBE Effect Parade").addProperty("ADBE Glo2");   // неоновое свечение цветом текста
    g.property("ADBE Glo2-0002").setValue(S.glow_threshold);
    g.property("ADBE Glo2-0003").setValue(S.glow_radius);
    g.property("ADBE Glo2-0004").setValue(S.glow_intensity);
    var sh2 = M.property("ADBE Effect Parade").addProperty("ADBE Drop Shadow");
    sh2.property("ADBE Drop Shadow-0002").setValue(150);
    sh2.property("ADBE Drop Shadow-0004").setValue(3);
    sh2.property("ADBE Drop Shadow-0005").setValue(14);
    reveal(M, ev.main, ev.start, ev.main.length);
    fadeOut(M, ev.end);
    made.push(M);
    return made;
}

// ---------------------------------------------------------------- плашка сверху
function addPlate(comp, pl) {
    var size = pl.size;
    var boxH = Math.round(size * 1.3 * pl.lines.length + size * 0.75);
    var box = comp.layers.addSolid([1, 1, 1], "Плашка", comp.width, boxH, 1, pl.dur);
    P(box, ["ADBE Transform Group", "ADBE Position"]).setValue([comp.width / 2, boxH / 2, 0]);
    box.startTime = 0; box.outPoint = pl.dur;
    for (var i = 0; i < pl.lines.length; i++) {
        var T = comp.layers.addText(pl.lines[i]);
        T.name = "Плашка: " + pl.lines[i];
        setText(T, pl.lines[i], pl.font, size, [0, 0, 0], 0);
        P(T, ["ADBE Transform Group", "ADBE Position"]).setValue(
            [comp.width / 2, Math.round(size * 0.38 + size * 0.8) + i * Math.round(size * 1.3), 0]);
        T.outPoint = pl.dur;
        fitWidth(T, 0.5, comp.width * 0.94);
    }
}

// ---------------------------------------------------------------- музыка
function addMusic(comp, m, folder) {
    var L = comp.layers.add(importFile(m.file, folder));
    L.name = "Музыка";
    L.startTime = 0;
    var lv = P(L, ["ADBE Audio Group", "ADBE Audio Levels"]);
    for (var i = 0; i < m.levels.length; i++) lv.setValueAtTime(m.levels[i][0], [m.levels[i][1], m.levels[i][1]]);
    return L;
}

// ---------------------------------------------------------------- сборка
function main() {
    var proj = app.newProject();
    var comp = proj.items.addComp(DATA.name, DATA.w, DATA.h, 1, DATA.total, DATA.fps);
    comp.frameRate = DATA.fps;
    comp.motionBlur = true;
    comp.shutterAngle = 180;
    comp.shutterPhase = -90;
    comp.bgColor = [0, 0, 0];
    var folder = proj.items.addFolder("Материалы");
    if (DATA.music) addMusic(comp, DATA.music, folder);
    for (var i = 0; i < DATA.clips.length; i++) addClip(comp, DATA.clips[i], folder);
    for (var f = 0; f < DATA.flashes.length; f++) addFlash(comp, DATA.flashes[f][0], DATA.flashes[f][1]);
    for (var s = 0; s < DATA.subs.length; s++) addSub(comp, DATA.subs[s], DATA.sub_style);
    if (DATA.plate) addPlate(comp, DATA.plate);
    var rq = proj.renderQueue.items.add(comp);
    var om = rq.outputModule(1);
    var tpl = om.templates, pick = "";
    for (var k = 0; k < tpl.length; k++) if (tpl[k].indexOf("H.264") >= 0 && tpl[k].indexOf("15") >= 0) pick = tpl[k];   // 15 Мбит/с — для YouTube 1440p достаточно
    if (pick) om.applyTemplate(pick);
    om.file = new File(DATA.render_to);
    comp.openInViewer();
    proj.save(new File(DATA.aep));
}

try {
    main();
} catch (err) {
    try {
        app.newProject();
        app.project.items.addComp("ERR|" + err.toString() + " (строка " + err.line + ")|", 100, 100, 1, 1, 24);
        app.project.save(new File(DATA.err_aep));
    } catch (e2) {}
}
if (DATA.quit) {
    // проект уже сохранён: закрываем его явно — иначе app.quit() иногда не срабатывает и AE остаётся открытым
    try { app.project.close(CloseOptions.DO_NOT_SAVE_CHANGES); } catch (e3) {}
    app.quit();
}
