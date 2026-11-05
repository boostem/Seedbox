/**
 * The waveform renderer: ONE WebGL2 context for the whole app (browsers allow ~16, and a library shows hundreds of
 * overviews). Each waveform is uploaded once as a texture (`mips.ts`: bands + peak mip chain) and kept in a small
 * LRU; a draw sets the uniforms (zoom, scroll, mode, playhead), renders into the shared offscreen canvas and copies
 * the pixels into the view's own 2D canvas with `drawImage`, a GPU-to-GPU copy. So views keep layering their grid,
 * cues and sections around the waveform on one canvas, and zooming or scrolling re-uploads nothing.
 *
 * `waveGL()` is null when WebGL2 isn't there (or the context is lost, or Settings turns the GPU off, or the URL has
 * `?wavegl=0`): `drawWave` then paints with Canvas 2D.
 */
import type { WaveData } from "../prepare";
import type { WaveMode } from "../settings";
import { useWaveSettings } from "../settings";
import { rgbOf, wavePalette } from "../palette";
import type { StemView, StemWave } from "../stems";
import { stemAtlas, waveAtlas, type WaveAtlas } from "./mips";
import { FRAGMENT, MAX_GL_LEVELS, STEM_FRAGMENT, VERTEX } from "./shaders";

const MODE_ID: Record<WaveMode, number> = { bands: 0, rgb: 1, stacked: 2 };
const MAX_TEXTURES = 48;
const VIEW_UNIFORMS = ["u_tex", "u_width", "u_rows", "u_lens", "u_levels", "u_first", "u_ppc", "u_height", "u_dpr", "u_split", "u_alpha"];

interface GlView {
  /** level-0 point at the left edge of column 0, and points per device column */
  first: number;
  ppc: number;
  /** device px of the target band */
  width: number;
  height: number;
  dpr: number;
  /** device x of the playhead (left of it is "played"); Infinity when there is none */
  split: number;
  alpha: number;
}

export interface GlDraw extends GlView {
  wave: WaveData;
  mode: WaveMode;
}

export interface GlStemDraw extends GlView {
  stems: StemWave;
  view: StemView;
  /** brightness of each stem, 0-1: drums, bass, vocals, other */
  bright: ArrayLike<number>;
}

interface Tex {
  tex: WebGLTexture;
  rows: Int32Array;
  lens: Int32Array;
  levels: number;
  width: number;
}

type Uniforms = Record<string, WebGLUniformLocation | null>;

export class WaveGL {
  readonly canvas: HTMLCanvasElement;
  private gl: WebGL2RenderingContext;
  private prog: WebGLProgram | null = null;
  private u: Uniforms = {};
  private stemProg: WebGLProgram | null = null;
  private su: Uniforms = {};
  private vao: WebGLVertexArrayObject | null = null;
  private textures = new Map<WaveData | StemWave, Tex>();
  private maxTex = 4096;
  lost = false;
  /** draws since start, for #/bench and tests */
  draws = 0;

  constructor(canvas: HTMLCanvasElement, gl: WebGL2RenderingContext) {
    this.canvas = canvas;
    this.gl = gl;
    canvas.addEventListener("webglcontextlost", (e) => {
      e.preventDefault();
      this.lost = true;
      this.textures.clear();
    });
    canvas.addEventListener("webglcontextrestored", () => {
      this.lost = !this.init();
    });
    this.lost = !this.init();
  }

  private init(): boolean {
    const gl = this.gl;
    const shader = (type: number, src: string) => {
      const s = gl.createShader(type)!;
      gl.shaderSource(s, src);
      gl.compileShader(s);
      if (!gl.getShaderParameter(s, gl.COMPILE_STATUS) && !gl.isContextLost()) throw new Error(`waveform shader: ${gl.getShaderInfoLog(s)}`);
      return s;
    };
    const link = (fragment: string, names: string[]): [WebGLProgram, Uniforms] | null => {
      const prog = gl.createProgram();
      if (!prog) return null;
      gl.attachShader(prog, shader(gl.VERTEX_SHADER, VERTEX));
      gl.attachShader(prog, shader(gl.FRAGMENT_SHADER, fragment));
      gl.linkProgram(prog);
      if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) {
        if (gl.isContextLost()) return null;
        throw new Error(`waveform program: ${gl.getProgramInfoLog(prog)}`);
      }
      const u: Uniforms = {};
      for (const name of [...VIEW_UNIFORMS, ...names]) u[name] = gl.getUniformLocation(prog, name);
      return [prog, u];
    };
    const wave = link(FRAGMENT, ["u_mode", "u_low", "u_mid", "u_high", "u_rgbLow", "u_rgbMid", "u_rgbHigh"]);
    const stems = link(STEM_FRAGMENT, ["u_view", "u_bright", "u_drums", "u_bass", "u_vocals", "u_other"]);
    if (!wave || !stems) return false;
    [this.prog, this.u] = wave;
    [this.stemProg, this.su] = stems;
    this.vao = gl.createVertexArray();
    this.maxTex = Math.min(8192, gl.getParameter(gl.MAX_TEXTURE_SIZE) as number);
    gl.disable(gl.BLEND);
    gl.disable(gl.DEPTH_TEST);
    return true;
  }

  /** The waveform's texture, uploaded on first use and kept while it is among the last MAX_TEXTURES drawn. */
  private texture(wave: WaveData | StemWave, atlasOf: () => WaveAtlas): Tex {
    const hit = this.textures.get(wave);
    if (hit) {
      this.textures.delete(wave);
      this.textures.set(wave, hit);
      return hit;
    }
    const gl = this.gl;
    const atlas = atlasOf();
    const tex = gl.createTexture()!;
    gl.bindTexture(gl.TEXTURE_2D, tex);
    gl.pixelStorei(gl.UNPACK_ALIGNMENT, 1);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA8, atlas.width, atlas.height, 0, gl.RGBA, gl.UNSIGNED_BYTE, atlas.data);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    const levels = Math.min(MAX_GL_LEVELS, atlas.rows.length);
    const rows = new Int32Array(MAX_GL_LEVELS);
    const lens = new Int32Array(MAX_GL_LEVELS);
    rows.set(atlas.rows.slice(0, levels));
    lens.set(atlas.lengths.slice(0, levels));
    const t: Tex = { tex, rows, lens, levels, width: atlas.width };
    this.textures.set(wave, t);
    while (this.textures.size > MAX_TEXTURES) {
      const [k, old] = this.textures.entries().next().value!;
      gl.deleteTexture(old.tex);
      this.textures.delete(k);
    }
    return t;
  }

  /** Sets up the viewport and the uniforms both programs share, with `prog` in use. */
  private begin(prog: WebGLProgram, u: Uniforms, t: Tex, d: GlView, w: number, h: number): void {
    const gl = this.gl;
    gl.viewport(0, 0, w, h);
    gl.clearColor(0, 0, 0, 0);
    gl.clear(gl.COLOR_BUFFER_BIT);
    gl.useProgram(prog);
    gl.bindVertexArray(this.vao);
    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, t.tex);
    gl.uniform1i(u.u_tex, 0);
    gl.uniform1i(u.u_width, t.width);
    gl.uniform1iv(u.u_rows, t.rows);
    gl.uniform1iv(u.u_lens, t.lens);
    gl.uniform1i(u.u_levels, t.levels);
    gl.uniform1f(u.u_first, d.first);
    gl.uniform1f(u.u_ppc, d.ppc);
    gl.uniform1f(u.u_height, h);
    gl.uniform1f(u.u_dpr, d.dpr);
    gl.uniform1f(u.u_split, Number.isFinite(d.split) ? d.split : 1e9);
    gl.uniform1f(u.u_alpha, d.alpha);
  }

  /** The drawing size of `d`, growing the shared canvas (only grows, in steps, so views of different sizes don't
   * reallocate the drawing buffer every draw). */
  private size(d: GlView): [number, number] {
    const w = Math.max(1, Math.min(this.maxTex, Math.ceil(d.width)));
    const h = Math.max(1, Math.min(this.maxTex, Math.ceil(d.height)));
    if (this.canvas.width < w) this.canvas.width = Math.min(this.maxTex, Math.ceil(w / 256) * 256);
    if (this.canvas.height < h) this.canvas.height = Math.min(this.maxTex, Math.ceil(h / 64) * 64);
    return [w, h];
  }

  /** Renders `d` into the shared canvas; returns the canvas region (bottom-left `width` × `height` device px). */
  render(d: GlDraw): boolean {
    const gl = this.gl;
    if (this.lost || !this.prog || gl.isContextLost()) return false;
    const [w, h] = this.size(d);
    const t = this.texture(d.wave, () => waveAtlas(d.wave, this.maxTex));
    const p = wavePalette();
    const u = this.u;
    this.begin(this.prog, u, t, d, w, h);
    gl.uniform1i(u.u_mode, MODE_ID[d.mode]);
    gl.uniform3fv(u.u_low, rgbOf(p.low));
    gl.uniform3fv(u.u_mid, rgbOf(p.mid));
    gl.uniform3fv(u.u_high, rgbOf(p.high));
    gl.uniform3fv(u.u_rgbLow, rgbOf(p.rgbLow));
    gl.uniform3fv(u.u_rgbMid, rgbOf(p.rgbMid));
    gl.uniform3fv(u.u_rgbHigh, rgbOf(p.rgbHigh));
    gl.drawArrays(gl.TRIANGLES, 0, 3);
    this.draws++;
    return true;
  }

  /** Renders a stem waveform, as `render`. */
  renderStems(d: GlStemDraw): boolean {
    const gl = this.gl;
    if (this.lost || !this.stemProg || gl.isContextLost()) return false;
    const [w, h] = this.size(d);
    const t = this.texture(d.stems, () => stemAtlas(d.stems, this.maxTex));
    const p = wavePalette();
    const u = this.su;
    this.begin(this.stemProg, u, t, d, w, h);
    gl.uniform1i(u.u_view, d.view === "lanes" ? 1 : 0);
    gl.uniform4f(u.u_bright, d.bright[0], d.bright[1], d.bright[2], d.bright[3]);
    gl.uniform3fv(u.u_drums, rgbOf(p.stem.drums));
    gl.uniform3fv(u.u_bass, rgbOf(p.stem.bass));
    gl.uniform3fv(u.u_vocals, rgbOf(p.stem.vocals));
    gl.uniform3fv(u.u_other, rgbOf(p.stem.other));
    gl.drawArrays(gl.TRIANGLES, 0, 3);
    this.draws++;
    return true;
  }

  /** The GPU behind the context ("ANGLE (Intel, Mesa Intel(R) Iris(R) Xe Graphics …)"), where the browser says. */
  gpuName(): string {
    const gl = this.gl;
    const info = gl.getExtension("WEBGL_debug_renderer_info");
    return String(gl.getParameter(info ? info.UNMASKED_RENDERER_WEBGL : gl.RENDERER));
  }

  /** Renders `d` and copies it into `ctx` (scaled to CSS px) at CSS (x, y). */
  drawInto(ctx: CanvasRenderingContext2D, d: GlDraw, x: number, y: number): boolean {
    return this.render(d) && this.blit(ctx, d, x, y);
  }

  /** Renders the stem waveform `d` and copies it into `ctx`, as `drawInto`. */
  drawStemsInto(ctx: CanvasRenderingContext2D, d: GlStemDraw, x: number, y: number): boolean {
    return this.renderStems(d) && this.blit(ctx, d, x, y);
  }

  private blit(ctx: CanvasRenderingContext2D, d: GlView, x: number, y: number): boolean {
    const w = Math.max(1, Math.min(this.maxTex, Math.ceil(d.width)));
    const h = Math.max(1, Math.min(this.maxTex, Math.ceil(d.height)));
    // the GL viewport sits at the bottom-left of the canvas; canvas images count rows from the top
    ctx.drawImage(this.canvas, 0, this.canvas.height - h, w, h, x, y, w / d.dpr, h / d.dpr);
    return true;
  }
}

let instance: WaveGL | null | undefined;

function create(): WaveGL | null {
  if (typeof document === "undefined") return null;
  if (typeof location !== "undefined" && new URLSearchParams(location.search).get("wavegl") === "0") return null;
  try {
    const canvas = document.createElement("canvas");
    canvas.width = 1024;
    canvas.height = 256;
    const gl = canvas.getContext("webgl2", { alpha: true, premultipliedAlpha: true, antialias: false, depth: false, stencil: false, preserveDrawingBuffer: true, powerPreference: "high-performance" });
    if (!gl) return null;
    return new WaveGL(canvas, gl);
  } catch (e) {
    console.warn("waveform: WebGL2 unavailable, using Canvas 2D", e);
    return null;
  }
}

/** The shared renderer, or null: no WebGL2, its context is lost, or Settings asks for Canvas 2D. */
export function waveGL(): WaveGL | null {
  if (!useWaveSettings.getState().gpu) return null;
  if (instance === undefined) instance = create();
  return instance && !instance.lost ? instance : null;
}

/** Which painter draws the waveforms now: for Settings and #/bench. */
export function waveRendererName(): "webgl2" | "canvas2d" {
  return waveGL() ? "webgl2" : "canvas2d";
}

/** "webgl2 (<GPU>)" or "canvas2d": what draws the waveforms, for #/bench's results. */
export function waveRendererInfo(): string {
  const gl = waveGL();
  return gl ? `webgl2 (${gl.gpuName()})` : "canvas2d";
}

/** Whether this browser can run the WebGL2 renderer at all (whatever Settings says). */
export function webgl2Available(): boolean {
  if (instance === undefined) instance = create();
  return !!instance && !instance.lost;
}
