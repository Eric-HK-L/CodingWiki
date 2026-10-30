#ifndef ENGINE_H
#define ENGINE_H

#ifdef __cplusplus
namespace phy {

class Engine {
public:
    explicit Engine(int threads);
    int run();
    int threads_;
};

}  // namespace phy
#endif

typedef struct phy_config {
    int threads;
    float scale;
} phy_config_t;

#ifdef __cplusplus
extern "C" {
#endif

int helper(int value);

#ifdef __cplusplus
}
#endif

#endif
