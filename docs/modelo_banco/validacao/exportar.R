# Exporta as saídas do pipeline R para CSV, para carregar no Postgres e
# comparar os indicadores calculados pelas views com os do R.
# Rodar na raiz do repositório:  Rscript docs/modelo_banco/validacao/exportar.R <pasta_de_saida>
suppressMessages({library(arrow); library(data.table)})
b <- "projeto_onibus/dados/processados/"
out <- commandArgs(trailingOnly = TRUE)[1]
dir.create(out, showWarnings = FALSE, recursive = TRUE)
rd <- function(t) as.data.table(dplyr::collect(open_dataset(file.path(b, t))))
utc <- function(x) ifelse(is.na(x), NA_character_, format(x, "%Y-%m-%d %H:%M:%OS3+00", tz = "UTC"))

a <- rd("auditoria_saidas")
d <- rd("diagnostico_saidas")
a <- d[, .(scheduled_trip_id, diagnostico_frota, detalhe_frota, veiculos_possiveis, lacunas_ids)][a, on = "scheduled_trip_id"]
fwrite(a[, .(scheduled_trip_id, service_date, line_id, direction_id, tipo_dia,
             scheduled_departure_utc = utc(scheduled_departure_utc),
             observed_departure_utc = utc(observed_departure_utc),
             departure_difference_minutes, observed_trip_id, observed_vehicle_id, audit_status,
             confidence_level, data_coverage_status, frac_min_dados, pct_tracado, pct_cobertura,
             evidence_summary, so_lacuna_aberta, schedule_quality_flag, tolerancia_min,
             diagnostico_frota, detalhe_frota,
             veiculos_possiveis = ifelse(is.na(veiculos_possiveis) | veiculos_possiveis == "", "{}",
                                         paste0("{", veiculos_possiveis, "}")),
             lacunas_ids)],
       file.path(out, "auditoria.csv"), na = "")

fwrite(rd("icv_faixas")[, .(line_id, service_date, tipo_dia, sentido, hora, periodo, programadas,
                            realizadas, quase_completas, inconclusivas)],
       file.path(out, "icv_faixas.csv"), na = "")

v <- rd("viagens_reconstruidas")
fwrite(v[, .(trip_id, line_id, vehicle_id, service_date, direction_id,
             inicio_obs = utc(as.POSIXct(t_ini_obs, origin = "1970-01-01", tz = "UTC")),
             fim_obs = utc(as.POSIXct(t_fim_obs, origin = "1970-01-01", tz = "UTC")),
             partida_em = utc(departure_time_utc), chegada_em = utc(arrival_time_utc),
             duration_minutes, distance_km, vel_media_kmh, observation_coverage, frac_observada,
             lacuna_max_s = round(lacuna_max_s), trajectory_status, confidence_level, route_match_status,
             origin_evidence, destination_evidence,
             alertas = ifelse(is.na(quality_flags) | quality_flags == "", "{}",
                              paste0("{", gsub(";", ",", quality_flags), "}")),
             gtfs_versao)],
       file.path(out, "viagens.csv"), na = "")

l <- rd("lacunas_veiculos")
fwrite(l[, .(gid, line_id, vehicle_id, service_date, inicio_em = utc(t0_utc),
             fim_em = ifelse(aberta, NA_character_, utc(t1_utc)), s0_km, s1_km, viagens_max, tipo)],
       file.path(out, "lacunas.csv"), na = "")

p <- rd("posicoes_normalizadas")
fwrite(p[, .(horario_gps = utc(timestamp_utc), prefixo = vehicle_id, line_id, cl, direction_sl, latitude, longitude,
             qc_dup_instante, qc_dup_exato, qc_coord_invalida, qc_ts_invalido)],
       file.path(out, "posicoes.csv"), na = "")

cat("ok:", nrow(a), "saídas,", nrow(v), "viagens,", nrow(l), "lacunas,", nrow(p), "posições\n")
cat("dup instante:", sum(p$qc_dup_instante), " dup exato:", sum(p$qc_dup_exato),
    " coord inválida:", sum(p$qc_coord_invalida), " ts inválido:", sum(p$qc_ts_invalido), "\n")
