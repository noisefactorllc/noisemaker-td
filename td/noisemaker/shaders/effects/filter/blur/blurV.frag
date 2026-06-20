// NM_INPUTS: inputTex=0
// NM_OUTPUT: fragColor
#define inputTex sTD2DInputs[0]
/*
 * Vertical Gaussian blur pass
 */


uniform vec2 tileOffset;
uniform vec2 fullResolution;

uniform float radiusY;
uniform float renderScale;

out vec4 fragColor;

const float PI = 3.14159265359;

void nm_main() {
    vec2 globalCoord = gl_FragCoord.xy + tileOffset;
    ivec2 texSize = textureSize(inputTex, 0);
    vec2 uv = gl_FragCoord.xy / vec2(texSize);
    vec2 texelSize = 1.0 / vec2(texSize);

    int radius = int(radiusY * renderScale);
    if (radius <= 0) {
        fragColor = texture(inputTex, uv);
        return;
    }
    
    // Compute sigma for Gaussian (radius ~= 3*sigma)
    float sigma = float(radius) / 3.0;
    float sigma2 = sigma * sigma;
    
    vec4 sum = vec4(0.0);
    float weightSum = 0.0;
    
    for (int i = -radius; i <= radius; i++) {
        float x = float(i);
        float weight = exp(-(x * x) / (2.0 * sigma2));
        vec2 offset = vec2(0.0, float(i) * texelSize.y);
        sum += texture(inputTex, uv + offset) * weight;
        weightSum += weight;
    }
    
    fragColor = sum / weightSum;
}
void main() {
    nm_main();
    fragColor = TDOutputSwizzle(fragColor);
}
