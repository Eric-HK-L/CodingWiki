cmake_minimum_required(VERSION 3.20)
project(sample LANGUAGES C CXX CUDA)

set(PHY_SRC src/engine.c src/helper.c)
add_library(phy STATIC ${PHY_SRC})
target_include_directories(phy PUBLIC src)
target_compile_definitions(phy PRIVATE PHY_VERSION=1)

add_executable(sample_app src/main.cu)
target_link_libraries(sample_app PRIVATE phy)

add_test(NAME sample_smoke COMMAND sample_app)
