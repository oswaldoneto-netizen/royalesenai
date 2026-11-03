// Gera o map_data.json a partir do index.html:  node tools/gerar_mapa.js static/index.html 20261103 map_data.json
// (so precisa rodar de novo se voce mudar a geracao do mapa no index.html)
const vm = require('vm'), fs = require('fs');
function P(name) {
  const f = function () {};
  return new Proxy(f, {
    get(t, k) {
      if (k === Symbol.toPrimitive) return () => 0;
      if (k === Symbol.iterator) return function* () {};
      if (k === 'length') return 0;
      return P(name + '.' + String(k));
    },
    set() { return true },
    apply() { return P(name + '()') },
    construct() { return P('new ' + name) },
  });
}
function load(htmlPath, extra) {
  const html = fs.readFileSync(htmlPath, 'utf8');
  const code = html.match(/<script>([\s\S]*?)<\/script>/)[1];
  const sb = {
    document: P('document'), window: P('window'), THREE: P('THREE'), navigator: { maxTouchPoints: 0 },
    localStorage: { getItem: () => '', setItem() {}, removeItem() {} },
    fetch: () => ({ then() { return { then() { return { catch() {} }; }, catch() {} }; }, catch() {} }),
    addEventListener() {}, innerWidth: 1280, innerHeight: 720, devicePixelRatio: 1,
    requestAnimationFrame() {}, setInterval() {}, setTimeout() {}, clearTimeout() {},
    performance: { now: () => Date.now() }, location: { protocol: 'http:', host: 'x', reload() {} },
    screen: {}, WebSocket: function () { this.readyState = 1; this.send = (s) => (sb.__sent.push(s)); },
    console, Math, JSON, Date, Promise, Map, Set, Array, Object, Number, String, Error,
    __sent: [],
  };
  sb.window = sb; sb.self = sb;
  const ctx = vm.createContext(sb);
  vm.runInContext(code + '\n;this.__api={build,on,get OB(){return OB},get phase(){return phase},get others(){return others},get me(){return me},get lootM(){return lootM},get chestM(){return chestM},get inv(){return inv}};', ctx, { filename: 'index-script.js' });
  return sb;
}
module.exports = { load };
if (require.main === module) {
  const [, , html, seed, out] = process.argv;
  const sb = load(html);
  sb.__api.build(+seed);
  const OB = sb.__api.OB;
  const ob = OB.map((o, i) => {
    if (o.i !== i) throw new Error('indice divergente');
    const r = { k: o.k, x: o.x, z: o.z, h: o.h, hp: o.hp >= 1e8 ? 1e9 : o.hp, tp: o.tp };
    if (o.k === 0) r.r = o.r; else { r.hx = o.hx; r.hz = o.hz }
    return r;
  });
  fs.writeFileSync(out, JSON.stringify({ seed: +seed, ob }));
  console.log('obstaculos:', ob.length, 'arvores:', ob.filter(o => o.tp === 'tree').length, 'pedras:', ob.filter(o => o.tp === 'rock').length, 'predios:', ob.filter(o => o.tp === 'bld').length);
}
