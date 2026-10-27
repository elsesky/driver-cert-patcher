/*
 * gridd-unlock-patcher (mini) - Patch NVIDIA vGPU nvxdapix.dll with a custom NLS root CA.
 *
 * Minimal PE parser + patcher, no third-party dependencies (no LIEF).
 * Logic mirrors gridd-unlock-patcher.cpp:
 *   1. Read whole DLL into memory.
 *   2. Manually parse DOS/PE header to locate .rdata and .data sections (file offset + RVA).
 *   3. Find the two hardcoded NLS root CA base64 strings inside .rdata.
 *   4. Replace the first with the user-provided certificate (PEM text with the
 *      BEGIN/END lines and newlines stripped, exactly like the original tool).
 *   5. Zero out the rest of the space between cert1 and cert2.
 *   6. Erase the reference (8-byte pointer) to the second certificate inside .data.
 *   7. Rename "X-NLS-Signature" to "x-nls-signature" (HTTP/2 compliance).
 *   8. Write the patched bytes back to the file.
 *
 * Build (MinGW):  x86_64-w64-mingw32-gcc -O2 -o gridd-unlock-patcher.exe gridd-unlock-patcher-mini.c
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>

static FILE *gridd_filep = NULL;
static uint8_t *gridd_data = NULL;
static long gridd_size = 0;

static FILE *cert_fp = NULL;

/* Hardcoded NLS root CA base64 strings (Windows guest = .dll, no PEM header/newlines).
 * Source: nls-root_ca-certificates.hpp (gridd_hardcoded_cert_one_windows / _two_windows). */
static const char *gridd_hardcoded_cert_one =
    "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"
    "bBj+W2v1REPS";

static const char *gridd_hardcoded_cert_two =
    "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"
    "JQ6S82i+o497";

/* ------------------------- minimal PE parsing ------------------------- */

/* Find the byte offset of `pattern` inside [hay, hay+hlen), scanning up to
 * `limit` bytes from the start of hay (limit can be hlen to scan the whole
 * section). Returns the offset from hay, or -1. */
static long memsearch(const uint8_t *hay, long hlen, const uint8_t *pat, long plen, long limit)
{
    long end = hlen;
    if (limit > 0 && limit < end)
        end = limit;
    if (plen <= 0 || plen > end)
        return -1;
    const uint8_t *stop = hay + end - plen;
    const uint8_t first = pat[0];
    for (const uint8_t *p = hay; p <= stop; p++) {
        if (*p == first && memcmp(p, pat, (size_t)plen) == 0)
            return (long)(p - hay);
    }
    return -1;
}

/* Find the first section whose name matches. Fills *off (raw data file
 * offset) and *rva (relative virtual address). Returns 1 on success. */
static int pe_find_section(uint16_t num, const uint8_t *sechead, const char *name,
                           long *off, long *rva)
{
    size_t i, nlen = strlen(name);
    for (i = 0; i < num; i++) {
        const uint8_t *s = sechead + i * 40;
        /* Section names are 8 bytes, NUL-padded, not necessarily NUL-terminated
         * within the 8-byte field. Compare only the actual name length. */
        if (nlen <= 8 && memcmp(s, name, nlen) == 0) {
            *off = *(int32_t *)(s + 20);
            *rva = *(int32_t *)(s + 12);
            return 1;
        }
    }
    return 0;
}

/* ------------------------- user certificate ------------------------- */

#define PEM_BEGIN_CERTIFICATE "-----BEGIN CERTIFICATE-----"
#define PEM_END_CERTIFICATE   "-----END CERTIFICATE-----"

/* Read the user PEM certificate, strip the BEGIN/END marker lines and all
 * newlines, produce the plain base64 blob that goes into the .rdata space.
 * Mirrors read_user_cert() in the original tool. */
static int read_user_cert(uint8_t **out, size_t *out_size)
{
    long sz;
    uint8_t *buf;
    char *tmp;
    size_t i, n;

    fseek(cert_fp, 0, SEEK_END);
    sz = ftell(cert_fp);
    fseek(cert_fp, 0, SEEK_SET);
    if (sz <= 0)
        return -1;

    buf = (uint8_t *)malloc((size_t)sz + 1);
    if (!buf)
        return -1;
    if (fread(buf, 1, (size_t)sz, cert_fp) != (size_t)sz) {
        free(buf);
        return -1;
    }
    buf[sz] = 0;
    tmp = (char *)buf;

    if (strstr(tmp, PEM_BEGIN_CERTIFICATE) == NULL ||
        strstr(tmp, PEM_END_CERTIFICATE) == NULL) {
        free(buf);
        return -1;
    }

    /* Skip the BEGIN marker line, keep everything until the END marker. */
    tmp += strlen(PEM_BEGIN_CERTIFICATE);
    while (*tmp == '\r' || *tmp == '\n')
        tmp++;
    {
        const char *endp = strstr(tmp, PEM_END_CERTIFICATE);
        if (!endp) {
            free(buf);
            return -1;
        }
        n = (size_t)(endp - tmp);
    }

    *out_size = 0;
    for (i = 0; i < n; i++) {
        char c = tmp[i];
        if (c != '\n' && c != '\r')
            buf[(*out_size)++] = (uint8_t)c;
    }
    buf[*out_size] = 0;
    *out = buf;
    return 0;
}

/* ------------------------------ main ------------------------------ */

static void usage(const char *prog)
{
    printf("Usage: %s -g <nvxdapix.dll> -c <root-certificate.pem>\n", prog);
}

/* Minimal argument parser: -g <file> -c <file> (getopt is not in MinGW). */
static int parse_args(int argc, char **argv)
{
    int i;
    for (i = 1; i < argc; i++) {
        if (strcmp(argv[i], "-g") == 0) {
            if (i + 1 >= argc) { usage(argv[0]); return -1; }
            gridd_filep = fopen(argv[++i], "rb+");
        } else if (strcmp(argv[i], "-c") == 0) {
            if (i + 1 >= argc) { usage(argv[0]); return -1; }
            cert_fp = fopen(argv[++i], "rb");
        } else if (strcmp(argv[i], "-h") == 0) {
            usage(argv[0]);
            return -1;
        }
    }
    if (!gridd_filep || !cert_fp) {
        usage(argv[0]);
        return -1;
    }
    return 0;
}

int main(int argc, char **argv)
{
    long pe_off, num, optsize;
    const uint8_t *sechead;
    uint64_t imagebase;
    long rdata_off, rdata_rva, data_off, data_rva;
    const uint8_t *sec_base;
    long rdata_sz = 0, data_sz = 0;
    long cert_one_off, cert_two_off;
    long total_cert_size;
    uint8_t *user_cert = NULL;
    size_t user_cert_size = 0;
    uint64_t target_addr, cert_xref_off, xref_val;
    long xoff;

    if (parse_args(argc, argv) != 0)
        return -1;

    printf("gridd-unlock-patcher: Patching GRID daemons with custom NLS certificates.\n");

    /* ---- read the DLL ---- */
    fseek(gridd_filep, 0, SEEK_END);
    gridd_size = ftell(gridd_filep);
    fseek(gridd_filep, 0, SEEK_SET);
    if (gridd_size < 64) {
        printf("Failed to parse GRID executable! File too small.\n");
        return -1;
    }
    gridd_data = (uint8_t *)malloc((size_t)gridd_size);
    if (!gridd_data) {
        printf("Out of memory.\n");
        return -1;
    }
    if (fread(gridd_data, 1, (size_t)gridd_size, gridd_filep) != (size_t)gridd_size) {
        printf("Failed to read GRID executable.\n");
        return -1;
    }

    /* ---- parse DOS/PE headers ---- */
    if (memcmp(gridd_data, "MZ", 2) != 0) {
        printf("Failed to parse GRID executable! Not a PE file (no MZ header).\n");
        return -1;
    }
    pe_off = *(int32_t *)(gridd_data + 0x3C);
    if (pe_off < 0 || pe_off + 24 > gridd_size ||
        memcmp(gridd_data + pe_off, "PE\0\0", 4) != 0) {
        printf("Failed to parse GRID executable! No PE signature.\n");
        return -1;
    }
    if (*(uint16_t *)(gridd_data + pe_off + 24) != 0x20B) { /* PE32+ only (vGPU drivers are x64) */
        printf("Failed to parse GRID executable! Expected a 64-bit PE (PE32+).\n");
        return -1;
    }
    /* COFF header starts at pe_off+4:
     *   +0:  Machine (H)
     *   +2:  NumberOfSections (H)      -> pe_off + 6
     *   +4:  TimeDateStamp (I)
     *   +8:  PointerToSymbolTable (I)
     *   +12: NumberOfSymbols (I)
     *   +16: SizeOfOptionalHeader (H)  -> pe_off + 20
     *   +18: Characteristics (H)
     */
    num = *(uint16_t *)(gridd_data + pe_off + 6);
    optsize = *(uint16_t *)(gridd_data + pe_off + 20);
    if (num <= 0 || num > 96) {
        printf("Failed to parse GRID executable! Invalid section count %ld.\n", num);
        return -1;
    }
    sechead = gridd_data + pe_off + 24 + optsize;
    if (sechead + (size_t)num * 40 > gridd_data + gridd_size) {
        printf("Failed to parse GRID executable! Section table out of range.\n");
        return -1;
    }
    imagebase = *(uint64_t *)(gridd_data + pe_off + 48); /* OptionalHeader64.ImageBase: PE sig(4)+COFF(20)+OptHdr+24 */

    /* ---- locate .rdata and .data ---- */
    if (!pe_find_section((uint16_t)num, sechead, ".rdata", &rdata_off, &rdata_rva) ||
        !pe_find_section((uint16_t)num, sechead, ".data", &data_off, &data_rva)) {
        printf("Failed to parse GRID executable! Missing .rdata/.data sections - not a GRID executable?\n");
        return -1;
    }
    if (rdata_off < 0 || rdata_rva < 0 || data_off < 0 || data_rva < 0) {
        printf("Failed to parse GRID executable! Section offsets invalid.\n");
        return -1;
    }
    /* Section sizes: use SizeOfRawData (file size of the section), not VirtualSize.
     * VirtualSize can be smaller than the actual data we need to search. */
    {
        size_t i;
        rdata_sz = 0;
        data_sz = 0;
        for (i = 0; i < (size_t)num; i++) {
            const uint8_t *s = sechead + i * 40;
            if (memcmp(s, ".rdata", 6) == 0)
                rdata_sz = *(int32_t *)(s + 16); /* SizeOfRawData */
            if (memcmp(s, ".data", 5) == 0)
                data_sz = *(int32_t *)(s + 16); /* SizeOfRawData */
        }
    }
    sec_base = gridd_data + rdata_off;
    if (rdata_sz <= 0 || (size_t)rdata_off + (size_t)rdata_sz > (size_t)gridd_size) {
        printf("Failed to parse GRID executable! .rdata section out of range.\n");
        return -1;
    }
    if (data_sz <= 0 || (size_t)data_off + (size_t)data_sz > (size_t)gridd_size) {
        printf("Failed to parse GRID executable! .data section out of range.\n");
        return -1;
    }

    /* ---- find the two hardcoded certificates in .rdata ---- */
    cert_one_off = memsearch(sec_base, rdata_sz,
                             (const uint8_t *)gridd_hardcoded_cert_one,
                             (long)strlen(gridd_hardcoded_cert_one), rdata_sz);
    cert_two_off = memsearch(sec_base, rdata_sz,
                             (const uint8_t *)gridd_hardcoded_cert_two,
                             (long)strlen(gridd_hardcoded_cert_two), rdata_sz);
    if (cert_one_off < 0 || cert_two_off < 0) {
        printf("Failed to find the hardcoded NLS certificates! (one=%ld two=%ld)\n",
               cert_one_off, cert_two_off);
        return -1;
    }
    printf("Found the two hardcoded NLS certificates at 0x%lx and 0x%lx.\n",
           (unsigned long)(rdata_off + cert_one_off),
           (unsigned long)(rdata_off + cert_two_off));

    /* ---- size check ---- */
    fseek(cert_fp, 0, SEEK_END);
    {
        long cert_file_size = ftell(cert_fp);
        /* original: user_ca_size = st_size + 1 (trailing NUL) */
        if (cert_file_size < 0)
            return -1;
        total_cert_size = cert_two_off + (long)strlen(gridd_hardcoded_cert_two) + 1 - cert_one_off;
        if ((long)(cert_file_size + 1) > total_cert_size) {
            printf("The provided certificate (size %ld) is larger than available space (size %ld)!\n",
                   (long)(cert_file_size + 1), (long)total_cert_size);
            return -1;
        }
    }

    /* ---- read + normalize the user certificate ---- */
    if (read_user_cert(&user_cert, &user_cert_size) != 0) {
        printf("Failed to process provided certificate! Are you sure it's an OpenSSL PEM?\n");
        return -1;
    }
    if ((long)(user_cert_size + 1) > total_cert_size) {
        printf("The provided certificate (size %lu) is larger than available space (size %ld)!\n",
               (unsigned long)user_cert_size, (long)total_cert_size);
        return -1;
    }

    /* ---- replace the first certificate, NULL the rest ---- */
    {
        uint8_t *cert_start = gridd_data + rdata_off + cert_one_off;
        memcpy(cert_start, user_cert, user_cert_size + 1);
        memset(cert_start + user_cert_size + 1, 0,
               (size_t)(total_cert_size - (long)(user_cert_size + 1)));
    }
    printf("Replaced the hardcoded certificates with the provided one.\n");

    /* ---- erase the reference to the second certificate in .data ---- */
    target_addr = (uint64_t)imagebase + (uint64_t)rdata_rva + (uint64_t)cert_one_off;
    {
        uint8_t pat[8];
        memcpy(pat, &target_addr, 8);
        cert_xref_off = memsearch(gridd_data + data_off, data_sz, pat, 8, data_sz);
    }
    if (cert_xref_off < 0) {
        printf("Warning: could not find the certificate reference at 0x%llx in .data - skipping.\n",
               (unsigned long long)target_addr);
    } else {
        printf("Found the list of certificates at 0x%lx.\n",
               (unsigned long)(data_off + cert_xref_off));
        cert_xref_off += (long)sizeof(uint64_t);
        if (cert_xref_off + 8 <= data_sz) {
            xref_val = *(uint64_t *)(gridd_data + data_off + cert_xref_off);
            printf("Erasing the dangling reference to the old certificate at 0x%llx (Expect offset for Windows daemon).\n",
                   (unsigned long long)(xref_val - (uint64_t)imagebase));
            memset(gridd_data + data_off + cert_xref_off, 0, 8);
        }
    }

    /* ---- HTTP/2 header compliance ---- */
    xoff = memsearch(sec_base, rdata_sz,
                     (const uint8_t *)"X-NLS-Signature", 15, rdata_sz);
    if (xoff >= 0) {
        memcpy(gridd_data + rdata_off + xoff, "x-nls-signature", 15);
        printf("Patched the HTTP header \"X-NLS-Signature\" for HTTP/2 compliance.\n");
    }

    /* ---- write back ---- */
    fseek(gridd_filep, 0, SEEK_SET);
    if (fwrite(gridd_data, 1, (size_t)gridd_size, gridd_filep) != (size_t)gridd_size) {
        printf("Failed to write the patched DLL back!\n");
        return -1;
    }
    fflush(gridd_filep);

    free(user_cert);
    free(gridd_data);
    if (cert_fp)
        fclose(cert_fp);
    if (gridd_filep)
        fclose(gridd_filep);
    printf("Done!\n");
    return 0;
}
