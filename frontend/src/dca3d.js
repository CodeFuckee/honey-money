// 「运作年限 × 规模 × 年化」可旋转三维散点：原生 canvas 自绘引擎。
// 每只在市基金一个点；鼠标拖拽旋转、滚轮缩放、悬停显示基金明细——
// 类似三维模型查看器。不用 echarts-gl（echarts 6 不兼容）；25k 点
// 逐帧重投影在原生 canvas 上约 5ms，旋转流畅。
// 规模轴为对数刻度（0.02 亿 ~ 数千亿差 5 个数量级）。

const fmtPct = v => (v == null ? '—' : (v * 100).toFixed(1) + '%')

const X_MAX = 25          // 运作年限轴（年）
const Y_LOG_MIN = -2, Y_LOG_MAX = 4   // 规模轴 log10：0.01 亿 ~ 1 万亿
const Z_MIN = -1, Z_MAX = 3           // 年化轴：-100% ~ 300%（超出截断）
const XC = X_MAX / 2, YC = (Y_LOG_MIN + Y_LOG_MAX) / 2, ZC = (Z_MIN + Z_MAX) / 2

// 年化配色：低→高（灰蓝 → 蓝 → 绿，Carbon 色系）
function zColor(v) {
  const lo = -0.5, hi = 1.5
  const mix = (a, b, t) => Math.round(a + (b - a) * t)
  const t = Math.min(1, Math.max(0, (v - lo) / (hi - lo)))
  const c = t < 0.5
    ? [mix(105, 15, t * 2), mix(121, 98, t * 2), mix(119, 254, t * 2)]
    : [mix(15, 25, (t - 0.5) * 2), mix(98, 128, (t - 0.5) * 2), mix(254, 56, (t - 0.5) * 2)]
  return `rgb(${c[0]},${c[1]},${c[2]})`
}

const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v))

/**
 * 在 container 内挂载可旋转三维散点。
 * @param sc /api/funds/dca/scatter 响应 {points: [[age, scale, xirr, code, name, type]]}
 * @returns { destroy() }
 */
export function mountDca3DScatter(container, sc) {
  // 尺寸跟随容器实际宽高（移动端容器被媒体查询压到 ~430px 高，
  // 写死 620 会溢出压住下方结论卡）；下限仅防隐藏/0 尺寸退化
  const W = Math.max(300, container.clientWidth || 600)
  const H = Math.max(360, container.clientHeight || 620)
  const dpr = window.devicePixelRatio || 1
  const canvas = document.createElement('canvas')
  canvas.width = W * dpr
  canvas.height = H * dpr
  canvas.style.width = '100%'
  canvas.style.maxWidth = `${W}px`
  canvas.style.height = `${H}px`
  canvas.style.cursor = 'grab'
  container.style.position = 'relative'
  container.appendChild(canvas)
  const ctx = canvas.getContext('2d')
  ctx.scale(dpr, dpr)

  // ---- 数据：居中世界坐标 + 颜色分桶（逐帧批量设色） ----
  const raw = sc.points.filter(p => p[0] != null && p[1] > 0 && p[2] != null)
  const world = new Array(raw.length)      // 居中世界坐标
  const infos = new Array(raw.length)      // 悬停元数据
  const NB = 12
  const buckets = Array.from({ length: NB }, () => [])
  // 世界坐标归一化到 [-0.5, 0.5]（各轴等比缩到单位立方体，旋转/缩放才有正确尺度）
  raw.forEach((p, i) => {
    world[i] = [
      clamp(p[0], 0, X_MAX) / X_MAX - 0.5,
      (clamp(Math.log10(p[1]), Y_LOG_MIN, Y_LOG_MAX) - Y_LOG_MIN) / (Y_LOG_MAX - Y_LOG_MIN) - 0.5,
      (clamp(p[2], Z_MIN, Z_MAX) - Z_MIN) / (Z_MAX - Z_MIN) - 0.5,
    ]
    infos[i] = { age: p[0], scale: p[1], xirr: p[2], code: p[3], name: p[4], type: p[5] }
    const bi = Math.min(NB - 1, Math.max(0,
      Math.floor((p[2] + 0.5) / 2 * NB)))
    buckets[bi].push(i)
  })
  const bucketColors = Array.from({ length: NB }, (_, i) =>
    zColor(-0.5 + (i + 0.5) / NB * 2))

  // ---- 视角状态（转台相机：yaw 绕年化竖轴，pitch 俯仰） ----
  let yaw = -0.62, pitch = 0.30, zoom = 1
  let cyw = 1, syw = 0, cp = 1, sp = 0
  const span = Math.min(W - 190, H - 150) * 0.78
  function updateRot() {
    cyw = Math.cos(yaw); syw = Math.sin(yaw)
    cp = Math.cos(pitch); sp = Math.sin(pitch)
  }
  updateRot()
  function proj(x, y, z) {
    const x1 = x * cyw - y * syw
    const y1 = x * syw + y * cyw
    return {
      sx: W / 2 + x1 * span * zoom,
      sy: H / 2 - (z * cp - y1 * sp) * span * zoom,
    }
  }

  // 轴世界坐标（轴从共同原点出发）
  const O = [-0.5, -0.5, -0.5]
  const E_X = [0.5, -0.5, -0.5]
  const E_Y = [-0.5, 0.5, -0.5]
  const E_Z = [-0.5, -0.5, 0.5]
  const wAge = a => [a / X_MAX - 0.5, -0.5, -0.5]
  const wScale = l => [-0.5, (l - Y_LOG_MIN) / (Y_LOG_MAX - Y_LOG_MIN) - 0.5, -0.5]
  const wXirr = z => [-0.5, -0.5, (z - Z_MIN) / (Z_MAX - Z_MIN) - 0.5]
  const ageTicks = [0, 5, 10, 15, 20, 25]
  const scaleTicks = [-2, -1, 0, 1, 2, 3]
  const scaleTickLabel = { [-2]: '0.01', [-1]: '0.1', 0: '1', 1: '10', 2: '100', 3: '1000' }
  const xirrTicks = [-1, -0.5, 0, 0.5, 1, 1.5, 2, 3]

  const screenPts = new Array(raw.length)  // 悬停命中缓存（与 infos 对齐）

  function render() {
    ctx.clearRect(0, 0, W, H)
    ctx.font = '11px sans-serif'

    // 三维包围盒：墙面底色 + 地板/两壁网格（全部归一化坐标）
    const xLo = -0.5, xHi = 0.5, yLo = -0.5, yHi = 0.5, zLo = -0.5, zHi = 0.5
    const ageTicks = [0, 5, 10, 15, 20, 25]
    const scaleTicks = [-2, -1, 0, 1, 2, 3]
    const scaleTickLabel = { [-2]: '0.01', [-1]: '0.1', 0: '1', 1: '10', 2: '100', 3: '1000' }
    const xirrTicks = [-1, -0.5, 0, 0.5, 1, 1.5, 2, 3]
    const ageN = a => a / X_MAX - 0.5
    const scaleN = l => (l - Y_LOG_MIN) / (Y_LOG_MAX - Y_LOG_MIN) - 0.5
    const xirrN = z => (z - Z_MIN) / (Z_MAX - Z_MIN) - 0.5
    const q = (x, y, z) => proj(x, y, z)
    const grid = 'rgba(0,0,0,0.09)', fill0 = 'rgba(0,0,0,0.035)',
          fillA = 'rgba(0,0,0,0.05)', fillB = 'rgba(0,0,0,0.075)'

    // 墙面底色：地板 / 后壁（规模=yHi）/ 右壁（年限=xHi）
    const wall = (pts, fill) => { ctx.fillStyle = fill; ctx.beginPath()
      pts.forEach((p, i) => i ? ctx.lineTo(p.sx, p.sy) : ctx.moveTo(p.sx, p.sy))
      ctx.closePath(); ctx.fill() }
    wall([q(xLo, yLo, zLo), q(xHi, yLo, zLo), q(xHi, yHi, zLo), q(xLo, yHi, zLo)], fill0)
    wall([q(xLo, yHi, zLo), q(xHi, yHi, zLo), q(xHi, yHi, zHi), q(xLo, yHi, zHi)], fillA)
    wall([q(xHi, yLo, zLo), q(xHi, yHi, zLo), q(xHi, yHi, zHi), q(xHi, yLo, zHi)], fillB)

    // 地板网格：年限线（纵深方向）+ 规模线（横向）
    ctx.strokeStyle = grid
    for (const a of ageTicks) { const p1 = q(ageN(a), yLo, zLo), p2 = q(ageN(a), yHi, zLo)
      ctx.beginPath(); ctx.moveTo(p1.sx, p1.sy); ctx.lineTo(p2.sx, p2.sy); ctx.stroke() }
    for (const l of scaleTicks) { const p1 = q(xLo, scaleN(l), zLo), p2 = q(xHi, scaleN(l), zLo)
      ctx.beginPath(); ctx.moveTo(p1.sx, p1.sy); ctx.lineTo(p2.sx, p2.sy); ctx.stroke() }
    // 后壁网格（规模=yHi 平面：竖线=年限，横线=年化）
    for (const a of ageTicks) { const p1 = q(ageN(a), yHi, zLo), p2 = q(ageN(a), yHi, zHi)
      ctx.beginPath(); ctx.moveTo(p1.sx, p1.sy); ctx.lineTo(p2.sx, p2.sy); ctx.stroke() }
    for (const x of xirrTicks) { const p1 = q(xLo, yHi, xirrN(x)), p2 = q(xHi, yHi, xirrN(x))
      ctx.beginPath(); ctx.moveTo(p1.sx, p1.sy); ctx.lineTo(p2.sx, p2.sy); ctx.stroke() }
    // 右壁网格（年限=xHi 平面：纵深线=规模，横线=年化）
    for (const l of scaleTicks) { const p1 = q(xHi, scaleN(l), zLo), p2 = q(xHi, scaleN(l), zHi)
      ctx.beginPath(); ctx.moveTo(p1.sx, p1.sy); ctx.lineTo(p2.sx, p2.sy); ctx.stroke() }
    for (const x of xirrTicks) { const p1 = q(xHi, yLo, xirrN(x)), p2 = q(xHi, yHi, xirrN(x))
      ctx.beginPath(); ctx.moveTo(p1.sx, p1.sy); ctx.lineTo(p2.sx, p2.sy); ctx.stroke() }

    // 散点（按颜色桶批量：一次设色 + 单 path，25k 逐帧可控）
    for (let bi = 0; bi < NB; bi++) {
      const idxs = buckets[bi]
      if (!idxs.length) continue
      ctx.fillStyle = bucketColors[bi]
      ctx.globalAlpha = 0.5
      ctx.beginPath()
      for (const i of idxs) {
        const w = world[i]
        const p = proj(w[0], w[1], w[2])
        ctx.moveTo(p.sx + 1.4, p.sy)
        ctx.arc(p.sx, p.sy, 1.4, 0, 6.2832)
      }
      ctx.fill()
      ctx.globalAlpha = 1
    }

    // 三条主轴（加粗）+ 刻度 + 数值 + 轴名
    const o = proj(...O), ex = proj(...E_X), ey = proj(...E_Y), ez = proj(...E_Z)
    ctx.strokeStyle = '#555'; ctx.lineWidth = 1.4
    ctx.beginPath(); ctx.moveTo(o.sx, o.sy); ctx.lineTo(ex.sx, ex.sy); ctx.stroke()
    ctx.beginPath(); ctx.moveTo(o.sx, o.sy); ctx.lineTo(ey.sx, ey.sy); ctx.stroke()
    ctx.beginPath(); ctx.moveTo(o.sx, o.sy); ctx.lineTo(ez.sx, ez.sy); ctx.stroke()
    ctx.lineWidth = 1
    ctx.fillStyle = '#525252'
    ctx.textAlign = 'center'
    for (const a of ageTicks) {
      const p = proj(...wAge(a))
      ctx.fillText(`${a}`, p.sx, p.sy + 14)
    }
    ctx.fillText('运作年限（年）', (o.sx + ex.sx) / 2, Math.max(o.sy, ex.sy) + 30)
    for (const l of scaleTicks) {
      const p = proj(...wScale(l))
      ctx.textAlign = 'right'
      ctx.fillText(scaleTickLabel[l], p.sx - 6, p.sy + 4)
    }
    ctx.textAlign = 'left'
    ctx.fillText('规模（亿，对数）→', ey.sx + 6, ey.sy + 2)
    ctx.textAlign = 'right'
    for (const z of xirrTicks) {
      if (z === Z_MIN) continue      // 原点角与规模 0.01 标签共用，跳过避免重叠
      const p = proj(...wXirr(z))
      ctx.fillText(`${Math.round(z * 100)}%`, p.sx - 6, p.sy + 4)
    }
    ctx.fillText('定投年化（XIRR）↑', ez.sx + 6, ez.sy - 8)
    ctx.textAlign = 'right'
    ctx.fillText('点色 = 年化：蓝低 → 绿高 ｜ 拖拽旋转 · 滚轮缩放', W - 14, 18)

    // 悬停命中缓存
    for (let i = 0; i < raw.length; i++) {
      const w = world[i]
      const p = proj(w[0], w[1], w[2])
      screenPts[i] = [p.sx, p.sy]
    }
  }

  // ---- 交互：拖拽旋转 / 滚轮缩放 / 悬停提示 ----
  const tooltip = document.createElement('div')
  tooltip.style.cssText = 'position:absolute;display:none;pointer-events:none;' +
    'background:rgba(255,255,255,0.97);border:1px solid #c6c6c6;border-radius:6px;' +
    'padding:7px 10px;font-size:12px;line-height:1.6;color:#161616;' +
    'box-shadow:0 2px 8px rgba(0,0,0,0.15);z-index:10;white-space:nowrap'
  container.appendChild(tooltip)

  let dragging = false, lastX = 0, lastY = 0
  function onDown(e) {
    dragging = true; lastX = e.clientX; lastY = e.clientY
    canvas.style.cursor = 'grabbing'
    tooltip.style.display = 'none'
  }
  function onMove(e) {
    if (dragging) {
      yaw += (e.clientX - lastX) * 0.008
      pitch = clamp(pitch + (e.clientY - lastY) * 0.006, -0.05, 1.25)
      lastX = e.clientX; lastY = e.clientY
      updateRot(); render()
      return
    }
    const rect = canvas.getBoundingClientRect()
    const mx = e.clientX - rect.left, my = e.clientY - rect.top
    let best = -1, bestD = 100          // 10px 半径（平方）
    for (let i = 0; i < screenPts.length; i++) {
      const p = screenPts[i]
      if (!p) continue
      const dx = p[0] - mx, dy = p[1] - my
      const d = dx * dx + dy * dy
      if (d < bestD) { bestD = d; best = i }
    }
    if (best >= 0) {
      const c = infos[best]
      tooltip.innerHTML = `<b>${c.code} ${c.name}</b><br/>${c.type || '—'}<br/>` +
        `运作年限：${c.age} 年 ｜ 规模：${c.scale} 亿<br/>定投年化（XIRR）：${fmtPct(c.xirr)}`
      tooltip.style.display = 'block'
      tooltip.style.left = Math.min(screenPts[best][0] + 14, W - 240) + 'px'
      tooltip.style.top = (screenPts[best][1] + 10) + 'px'
    } else {
      tooltip.style.display = 'none'
    }
  }
  function onUp() { dragging = false; canvas.style.cursor = 'grab' }
  function onWheel(e) {
    e.preventDefault()
    zoom = clamp(zoom * (e.deltaY < 0 ? 1.12 : 0.89), 0.4, 4)
    render()
  }
  canvas.addEventListener('pointerdown', onDown)
  window.addEventListener('pointermove', onMove)
  window.addEventListener('pointerup', onUp)
  canvas.addEventListener('wheel', onWheel, { passive: false })

  render()

  return {
    destroy() {
      canvas.removeEventListener('pointerdown', onDown)
      window.removeEventListener('pointermove', onMove)
      window.removeEventListener('pointerup', onUp)
      canvas.removeEventListener('wheel', onWheel)
      canvas.remove()
      tooltip.remove()
    },
  }
}
