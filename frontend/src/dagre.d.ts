// dagre 0.8.x 的 CommonJS 主入口在 Vite 2 生产打包时 graphlib/lodash 的
// CJS interop 会失效（graphlib 变成 undefined）。改用自带完整依赖的 UMD
// 浏览器包 dagre/dist/dagre.min.js 规避，此处为它补一个类型声明，
// 让 tsc 能解析默认导入（API 与主入口完全一致）。
declare module 'dagre/dist/dagre.min.js' {
  import dagre from 'dagre';
  export default dagre;
}
