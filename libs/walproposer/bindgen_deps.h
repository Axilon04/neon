/*
 * This header file is the input to bindgen. It includes all the
 * PostgreSQL headers that we need to auto-generate Rust structs
 * from. If you need to expose a new struct to Rust code, add the
 * header here, and whitelist the struct in the build.rs file.
 */
// #include "c.h"
// #include "walproposer.h"

int TestFunc(int a, int b);
