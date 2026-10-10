/* SPDX-License-Identifier: MIT
 * Compile-time SDK 27 annotation bridge for the Darwin 20 bootstrap sources.
 * This supplies header annotations only; it supplies no runtime entry points.
 * The alternative SDK profile remains experimental until ABI/runtime tests pass.
 */
#ifndef BLAUOS_SDK27_COMPILE_H
#define BLAUOS_SDK27_COMPILE_H
#ifndef OS_ASSUME_PTR_ABI_SINGLE_BEGIN
#define OS_ASSUME_PTR_ABI_SINGLE_BEGIN
#endif
#ifndef OS_ASSUME_PTR_ABI_SINGLE_END
#define OS_ASSUME_PTR_ABI_SINGLE_END
#endif
#ifndef __ASSUME_PTR_ABI_SINGLE_BEGIN
#define __ASSUME_PTR_ABI_SINGLE_BEGIN
#endif
#ifndef __ASSUME_PTR_ABI_SINGLE_END
#define __ASSUME_PTR_ABI_SINGLE_END
#endif
#ifndef OS_WORKGROUP_UNSAFE_INDEXABLE
#define OS_WORKGROUP_UNSAFE_INDEXABLE
#endif
#ifndef __single
#define __single
#endif
#ifndef __unsafe_forge_single
#define __unsafe_forge_single(T, P) ((T)(P))
#endif
#ifndef __unsafe_indexable
#define __unsafe_indexable
#endif
#ifndef OS_UNSAFE_INDEXABLE
#define OS_UNSAFE_INDEXABLE
#endif
#ifndef __swift_nonisolated_unsafe
#define __swift_nonisolated_unsafe
#endif
#ifndef OS_SWIFT_UNAVAILABLE_FROM_ASYNC
#define OS_SWIFT_UNAVAILABLE_FROM_ASYNC(x)
#endif
#ifndef __API_AVAILABLE_PLATFORM_visionos
#define __API_AVAILABLE_PLATFORM_visionos(x) visionos,introduced=x
#endif
#ifndef __API_UNAVAILABLE_PLATFORM_visionos
#define __API_UNAVAILABLE_PLATFORM_visionos visionos,unavailable
#endif
#endif
#ifndef __alloc_align
#define __alloc_align(x) __attribute__((__alloc_align__(x)))
#endif
