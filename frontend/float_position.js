const FLOAT_WIDTH = 88;
const FLOAT_HEIGHT = 88;
const FLOAT_AVATAR_INSET = 12;
const FLOAT_SHAPE_INSET = 8;
const MIN_VISIBLE = FLOAT_WIDTH;

function createCircleShape(
  diameter = FLOAT_WIDTH - FLOAT_SHAPE_INSET * 2,
  inset = FLOAT_SHAPE_INSET,
) {
  diameter = Math.round(Number(diameter));
  inset = Math.round(Number(inset));
  if (diameter < 1 || inset < 0) throw new RangeError("invalid circle shape");
  const radius = diameter / 2;
  return Array.from({length: diameter}, (_, y) => {
    const dy = y + 0.5 - radius;
    const halfWidth = Math.sqrt(radius * radius - dy * dy);
    const left = Math.ceil(radius - halfWidth - 0.5);
    const right = Math.floor(radius + halfWidth - 0.5);
    return {x: inset + left, y: inset + y, width: right - left + 1, height: 1};
  });
}

// Cover whole edge pixels so the CSS arc keeps its antialiasing inside the native region.
function createRoundedRectShape(width, height, radius = 16) {
  if (![width, height, radius].every(Number.isFinite) || width < 1 || height < 1 || radius < 0) {
    throw new RangeError("invalid rounded rectangle shape");
  }
  width = Math.round(width); height = Math.round(height);
  radius = Math.min(Math.round(radius), Math.floor(Math.min(width, height) / 2));
  if (!radius) return [{ x: 0, y: 0, width, height }];
  const rows = Array.from({ length: radius }, (_, y) => {
    const dy = radius - y - 1;
    const x = Math.floor(radius - Math.sqrt(radius * radius - dy * dy));
    return { x, y, width: radius * 2 - x * 2, height: 1 };
  });
  const shape = rows.flatMap(row => [
    { ...row, width: row.width + width - radius * 2 },
    { ...row, y: height - 1 - row.y, width: row.width + width - radius * 2 },
  ]);
  if (height > radius * 2) shape.push({ x: 0, y: radius, width, height: height - radius * 2 });
  return shape;
}

function clampFloatPosition(x, y, workArea) {
  const minX = workArea.x - (FLOAT_WIDTH - MIN_VISIBLE);
  const maxX = workArea.x + workArea.width - MIN_VISIBLE;
  const minY = workArea.y - (FLOAT_HEIGHT - MIN_VISIBLE);
  const maxY = workArea.y + workArea.height - MIN_VISIBLE;
  return {
    x: Math.round(Math.min(maxX, Math.max(minX, Number(x) || 0))),
    y: Math.round(Math.min(maxY, Math.max(minY, Number(y) || 0))),
  };
}

module.exports = {
  FLOAT_WIDTH,
  FLOAT_HEIGHT,
  FLOAT_AVATAR_INSET,
  FLOAT_SHAPE_INSET,
  clampFloatPosition,
  createCircleShape,
  createRoundedRectShape,
};
