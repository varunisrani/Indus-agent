export const version = "0.0.0";

export {
  BRAND_COLORS,
  BRAND_GRADIENT,
  BRAND_NAME,
  BRAND_SHORT,
  BRAND_WORDMARK,
  MARK_PATH,
  MARK_VIEWBOX,
  markSvg,
} from "./brand.ts";
export {
  type AnsiMap,
  type AnsiName,
  ansi,
  ansiNames,
} from "./tokens/ansi.ts";
export { cssPrefix, tokensToCss } from "./tokens/css.ts";
export {
  type DurationName,
  durations,
  type EasingName,
  easings,
  motionDurations,
} from "./tokens/motion.ts";
export {
  black,
  makeSwatch,
  type Oklch,
  oklchCss,
  oklchToHex,
  palette,
  type Ramp,
  type RampName,
  type RampStep,
  rampNames,
  rampSteps,
  type Swatch,
  white,
} from "./tokens/palette.ts";
export {
  CONTRAST_PAIRS,
  contrastFailures,
  contrastRatio,
  DEFAULT_PRESET,
  fitLightness,
  HEX_COLOUR,
  type PresetMode,
  type PresetRecipe,
  presetIds,
  presetRecipe,
  presets,
  presetTheme,
  relativeLuminance,
  themeCss,
  UserThemeError,
  type UserThemeInput,
  userTheme,
} from "./tokens/presets.ts";
export {
  isThemeName,
  type SemanticColour,
  type SemanticName,
  type SemanticTheme,
  semanticNames,
  type ThemeName,
  themeNames,
  themes,
} from "./tokens/semantic.ts";
export { type RadiusName, radii, type ShadowLevel, shadows, space } from "./tokens/space.ts";
export {
  type FontStackName,
  fontStacks,
  fontWeights,
  type TypeStep,
  type TypeStepName,
  typeScale,
} from "./tokens/type.ts";
