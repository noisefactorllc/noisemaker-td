// NM_INPUTS: statsTex=0
// NM_OUTPUT: fragColor
#define statsTex sTD2DInputs[0]
// Reindex Pass 2 (Reduce): collapse tile statistics to a global min/max pair.

const float F32_MAX = 3.402823466e38;
const float F32_MIN = -3.402823466e38;
const int TILE_SIZE = 8;
const int MAX_TILE_DIM = 512; // Supports resolutions up to 4096px.



out vec4 fragColor;

void nm_main() {
    // Single pixel output; ensure only the first fragment runs the reduction.
    if (int(gl_FragCoord.x) != 0 || int(gl_FragCoord.y) != 0) {
        fragColor = vec4(0.0);
        return;
    }

    ivec2 statsTexSize = textureSize(statsTex, 0);
    ivec2 tileCount = ivec2(
        (statsTexSize.x + TILE_SIZE - 1) / TILE_SIZE,
        (statsTexSize.y + TILE_SIZE - 1) / TILE_SIZE
    );

    float globalMin = F32_MAX;
    float globalMax = F32_MIN;

    for (int ty = 0; ty < MAX_TILE_DIM; ++ty) {
        if (ty >= tileCount.y) break;
        for (int tx = 0; tx < MAX_TILE_DIM; ++tx) {
            if (tx >= tileCount.x) break;
            ivec2 sampleCoord = ivec2(tx * TILE_SIZE, ty * TILE_SIZE);
            vec2 tileStats = texelFetch(statsTex, sampleCoord, 0).xy;
            globalMin = min(globalMin, tileStats.x);
            globalMax = max(globalMax, tileStats.y);
        }
    }

    fragColor = vec4(globalMin, globalMax, 0.0, 1.0);
}
void main() {
    nm_main();
    fragColor = TDOutputSwizzle(fragColor);
}
