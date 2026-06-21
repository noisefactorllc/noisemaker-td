// NM_INPUTS: preparedTex=0 rankTex=1 brightestTex=2
// NM_OUTPUT: fragColor
#define preparedTex sTD2DInputs[0]
#define rankTex sTD2DInputs[1]
#define brightestTex sTD2DInputs[2]
// GPGPU Pass 4: Gather sorted pixels with alignment
// Input: prepared texture (original colors), rank texture, brightest texture
// Output: Sorted row with brightest pixel aligned to its original position
// Uses approximate rank matching for efficiency





out vec4 fragColor;

void nm_main() {
    ivec2 coord = ivec2(gl_FragCoord.xy);
    ivec2 size = textureSize(preparedTex, 0);
    int x = coord.x;
    int y = coord.y;
    int width = size.x;
    
    // Get brightest x for this row
    float brightestXNorm = texelFetch(brightestTex, ivec2(0, y), 0).r;
    int brightestX = int(round(brightestXNorm * float(width - 1)));
    
    // Python algorithm:
    // sortedIndex = (x - brightestX + width) % width
    // Output position x gets the pixel whose rank == sortedIndex
    int sortedIndex = (x - brightestX + width) % width;
    float targetRank = float(sortedIndex) / float(width - 1);
    
    // Use sparse sampling to find a pixel with approximately matching rank
    // Instead of exact match, find the closest match
    const int NUM_SAMPLES = 64;
    float bestDiff = 2.0;
    int bestX = x;
    
    for (int s = 0; s < NUM_SAMPLES; s++) {
        int sampleX = (s * width) / NUM_SAMPLES;
        vec4 rankData = texelFetch(rankTex, ivec2(sampleX, y), 0);
        float pixelRank = rankData.r;
        
        float diff = abs(pixelRank - targetRank);
        if (diff < bestDiff) {
            bestDiff = diff;
            bestX = sampleX;
        }
    }
    
    // Fetch the color from the best matching pixel
    vec4 result = texelFetch(preparedTex, ivec2(bestX, y), 0);
    
    fragColor = result;
}
void main() {
    nm_main();
    fragColor = TDOutputSwizzle(fragColor);
}
