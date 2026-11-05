/**
 * GLSL for the waveform: one triangle over the viewport; the fragment shader finds its column's points, takes their
 * peak from the right mip level (or a cubic between points when zoomed in past one point per column) and colours the
 * pixel by the mode. Everything that changes with zoom, scroll and the playhead is a uniform.
 */

export const MAX_GL_LEVELS = 16;
/** The shape of a stem's peak before it is drawn (`stems.ts` draws with the same). */
export const STEM_CURVE = 0.8;

export const VERTEX = `#version 300 es
void main() {
  // one triangle that covers the viewport: (-1,-1), (3,-1), (-1,3)
  gl_Position = vec4(gl_VertexID == 1 ? 3.0 : -1.0, gl_VertexID == 2 ? 3.0 : -1.0, 0.0, 1.0);
}
`;

/** What both fragment shaders share: the uniforms of the atlas and the view, and how a column reads it. */
const COMMON = `#version 300 es
precision highp float;
precision highp int;

uniform sampler2D u_tex;         // the atlas (mips.ts)
uniform int u_width;             // atlas width in texels
uniform int u_rows[${MAX_GL_LEVELS}];   // first atlas row of each level
uniform int u_lens[${MAX_GL_LEVELS}];   // points in each level
uniform int u_levels;
uniform float u_first;           // level-0 point at the left edge of column 0
uniform float u_ppc;             // level-0 points per device column
uniform float u_height;          // viewport height, device px
uniform float u_dpr;
uniform float u_split;           // device x of the playhead: brighter left of it, dimmer right of it
uniform float u_alpha;

out vec4 outColor;

vec4 texel(int k, int i) {
  if (i < 0 || i >= u_lens[k]) return vec4(0.0);
  return texelFetch(u_tex, ivec2(i % u_width, u_rows[k] + i / u_width), 0);
}

// peak of level k over the level-0 points [a, b): at most 3 texels, as the level has 0.5-2 points per column
vec4 peak(int k, float a, float b) {
  float s = exp2(float(k));
  int i0 = int(floor(a / s));
  int i1 = max(i0 + 1, int(ceil(b / s)));
  vec4 m = vec4(0.0);
  for (int j = 0; j < 4; j++) {
    if (i0 + j >= i1) break;
    m = max(m, texel(k, i0 + j));
  }
  return m;
}

// level 0 at point position f (point i covers [i, i + 1)), Catmull-Rom between the points around it, kept between
// its two neighbours so it never rings above a peak: smooth curves at deep zoom instead of straight blobs
vec4 smoothAt(float f) {
  float x = f - 0.5;
  int i = int(floor(x));
  float t = x - float(i);
  vec4 p0 = texel(0, i - 1);
  vec4 p1 = texel(0, i);
  vec4 p2 = texel(0, i + 1);
  vec4 p3 = texel(0, i + 2);
  vec4 v = p1 + 0.5 * t * (p2 - p0 + t * (2.0 * p0 - 5.0 * p1 + 4.0 * p2 - p3 + t * (3.0 * (p1 - p2) + p3 - p0)));
  return clamp(v, min(p1, p2), max(p1, p2));
}

vec4 column(float c) {
  float a = u_first + c * u_ppc;
  float b = a + u_ppc;
  if (u_ppc <= 1.0) return smoothAt((a + b) * 0.5);
  float lv = min(log2(u_ppc), float(u_levels - 1));
  int k = int(floor(lv));
  vec4 v = peak(k, a, b);
  // blend into the next level as the zoom approaches it, so zooming never steps
  if (k + 1 < u_levels) v = mix(v, peak(k + 1, a, b), fract(lv));
  return v;
}

// one mirrored band of half-height h (device px) at distance d from its centre line: anti-aliased edge, a gradient
// that brightens towards the peak, and a soft glow just past the edge of loud columns; premultiplied
vec4 band(vec3 tint, float h, float d, float halfH) {
  h = max(h, 0.35 * u_dpr);
  float cover = clamp(h - d + 0.5, 0.0, 1.0);
  float g = clamp(d / max(h, 1.0), 0.0, 1.0);
  vec3 c = tint * mix(0.7, 1.0, g * g);
  float glow = d > h ? 0.2 * exp(-(d - h) / (2.5 * u_dpr)) * smoothstep(0.1, 0.6, h / halfH) : 0.0;
  float a = max(cover, glow);
  return vec4(c * a, a);
}

vec4 over(vec4 src, vec4 dst) {
  return src + dst * (1.0 - src.a);
}

// one solid layer of a stacked stem waveform: an anti-aliased edge, no glow
vec4 layer(vec3 tint, float h, float d) {
  float cover = clamp(h - d + 0.5, 0.0, 1.0);
  return vec4(tint * cover, cover);
}
`;

export const FRAGMENT = `${COMMON}
uniform int u_mode;              // 0 3-band, 1 RGB, 2 stacked
uniform vec3 u_low;
uniform vec3 u_mid;
uniform vec3 u_high;
uniform vec3 u_rgbLow;
uniform vec3 u_rgbMid;
uniform vec3 u_rgbHigh;

void main() {
  float c = floor(gl_FragCoord.x);
  float a = u_first + c * u_ppc;
  if (a + u_ppc <= 0.0 || a >= float(u_lens[0])) discard;  // outside the file
  vec4 v = column(c);
  float y = gl_FragCoord.y;
  vec4 col;
  if (u_mode == 2) {
    // stacked: three rows, lows at the bottom (gl y runs bottom-up), highs on top
    float rowH = u_height / 3.0;
    float r = min(2.0, floor(y / rowH));
    float amp = r < 0.5 ? pow(v.r, 0.85) : r < 1.5 ? pow(v.g, 1.25) : pow(v.b, 1.1);
    vec3 tint = r < 0.5 ? u_low : r < 1.5 ? u_mid : u_high;
    float halfH = rowH * 0.5 - u_dpr;
    col = band(tint, amp * halfH, abs(y - rowH * (r + 0.5)), halfH);
  } else if (u_mode == 1) {
    // RGB: one envelope, coloured by the balance of the bands, weighted like the 3-band gains (mids and highs are
    // dense in most music) and cubed, so the strongest one leads: kicks red, vocals and synths green, hats blue
    vec3 b = v.rgb * vec3(1.0, 0.72, 0.58);
    vec3 w = b * b * b + 1e-4;
    vec3 tint = (w.r * u_rgbLow + w.g * u_rgbMid + w.b * u_rgbHigh) / (w.r + w.g + w.b);
    tint /= max(max(tint.r, tint.g), max(tint.b, 0.4));
    float amp = pow(max(v.r, max(v.g * 0.9, v.b * 0.75)), 0.9);
    float halfH = u_height * 0.5 - 0.5 * u_dpr;
    col = band(tint, amp * halfH, abs(y - u_height * 0.5), halfH);
  } else {
    // 3-band, back to front: blue lows, amber mids, white highs (draw.ts drawWave2d has the same curves)
    float halfH = u_height * 0.5 - 0.5 * u_dpr;
    float d = abs(y - u_height * 0.5);
    col = band(u_low, pow(v.r, 0.85) * halfH, d, halfH);
    col = over(band(u_mid, pow(v.g, 1.6) * 0.64 * halfH, d, halfH) * 0.92, col);
    col = over(band(u_high, pow(v.b, 1.25) * 0.34 * halfH, d, halfH) * 0.88, col);
  }
  float lit = gl_FragCoord.x < u_split ? 1.0 : 0.55;
  outColor = col * lit * u_alpha;
}
`;

/**
 * Stems: the atlas holds R drums, G bass, B vocals, A other. Stacked is one waveform split into coloured layers (a
 * column's envelope is the energy sum of its stems, shared out by amplitude, bass at the centre and vocals on the
 * edge); lanes are four thin rows, drums on top. `stems.ts` has the same sums for the Canvas 2D painter. Layers are
 * opaque and a gain darkens their colour: alpha would let the layers behind show through.
 */
export const STEM_FRAGMENT = `${COMMON}
uniform int u_view;              // 0 stacked, 1 lanes
uniform vec4 u_bright;            // brightness of each stem now, atlas order
uniform vec3 u_drums;
uniform vec3 u_bass;
uniform vec3 u_vocals;
uniform vec3 u_other;

void main() {
  float c = floor(gl_FragCoord.x);
  float a = u_first + c * u_ppc;
  if (a + u_ppc <= 0.0 || a >= float(u_lens[0])) discard;  // outside the file
  vec4 v = pow(max(column(c), vec4(0.0)), vec4(${STEM_CURVE.toFixed(2)}));
  float y = gl_FragCoord.y;
  vec4 col;
  if (u_view == 1) {
    float rowH = u_height / 4.0;
    float r = min(3.0, floor((u_height - y) / rowH));  // gl y runs bottom-up, row 0 is the top one
    int i = int(r);
    vec3 tint = (i == 0 ? u_drums : i == 1 ? u_bass : i == 2 ? u_vocals : u_other) * u_bright[i];
    float halfH = rowH * 0.5 - u_dpr;
    float centre = u_height - rowH * (r + 0.5);
    col = band(tint, v[i] * halfH, abs(y - centre), halfH);
  } else {
    float halfH = u_height * 0.5 - 0.5 * u_dpr;
    float d = abs(y - u_height * 0.5);
    float total = v.r + v.g + v.b + v.a;
    float env = min(1.0, length(v)) * halfH / max(total, 1e-4);
    // outermost first, each layer over the one before: the edges between them stay anti-aliased
    float h = total * env;
    col = layer(u_vocals * u_bright.b, h, d);
    h -= v.b * env;
    col = over(layer(u_other * u_bright.a, h, d), col);
    h -= v.a * env;
    col = over(layer(u_drums * u_bright.r, h, d), col);
    h -= v.r * env;
    col = over(layer(u_bass * u_bright.g, h, d), col);
  }
  float lit = gl_FragCoord.x < u_split ? 1.0 : 0.55;
  outColor = col * lit * u_alpha;
}
`;
