#include "engine.h"
#include <cstdio>
#include <cmath>

__global__ void add_kernel(float* out, const float* a, const float* b, int n) {
    int i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i < n) {
        out[i] = a[i] + b[i];
    }
}

__device__ float scale(float value) {
    return value * 2.0f;
}

namespace phy {

Engine::Engine(int threads) : threads_(threads) {}

int Engine::run() {
    const char* level = std::getenv("PHY_LOG_LEVEL");
    if (level == nullptr) {
        return -1;
    }
    add_kernel<<<1, 32>>>(nullptr, nullptr, nullptr, 0);
    return helper(threads_);
}

}  // namespace phy

int main() {
    phy::Engine engine(4);
    return engine.run();
}
