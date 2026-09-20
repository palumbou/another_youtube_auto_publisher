> **Sviluppo assistito dall'intelligenza artificiale**  
> Questo progetto utilizza strumenti di intelligenza artificiale durante lo sviluppo, sotto continua supervisione umana. Le decisioni architetturali, editoriali, di sicurezza e di rilascio restano soggette a revisione umana.

# another-youtube-auto-publisher

Pipeline di pubblicazione YouTube su AWS con revisione obbligatoria. Il produttore
carica un video master e un manifest sotto `incoming/{project}/{job}/` e scrive
`READY` per ultimo; la pipeline valida il job rispetto al contratto versionato,
trascrive e analizza, renderizza Shorts 9:16 e presenta tutto in un pannello
privato. Nulla viene caricato senza approvazione del proprietario; gli asset
approvati vengono caricati **privati**, secondo pianificazione, con l'autorizzazione
OAuth del proprietario del canale.

> **Lingue disponibili**: [English](README.md) | [Italiano (corrente)](README.it.md)

## Regole non negoziabili

- Solo un marker `READY` creato dopo il `cutover_at` immutabile genera un job. Gli
  oggetti già presenti e i video già sul canale non vengono mai importati né analizzati.
- Il manifest (`contracts/video-job-manifest/1.0.0/schema.json`) è autoritativo per
  confini delle domande, diritti e flag editoriali; l'analisi arricchisce, non sovrascrive.
- Approvazione umana obbligatoria per ogni asset. Il publisher non rende mai pubblico
  un video; `publishAt` viene richiesto solo se il progetto API Google risulta verificato.
- Un interruttore (`PUBLISH_KILL_SWITCH`, attivo per impostazione predefinita) sospende
  la pubblicazione senza toccare la revisione.
- Nessuna credenziale nel repository: i segreti OAuth stanno in Secrets Manager e solo
  la funzione di pubblicazione può leggerli.

## Sviluppo locale (senza AWS, senza pubblicazioni reali)

```bash
make setup     # ambiente e dipendenze
make test      # ruff + pytest (inclusi render reali con ffmpeg)
make e2e       # fixture generata: ingestione → revisione → upload finto
make serve     # pannello su http://127.0.0.1:8080 con utente locale
```

La documentazione completa (architettura, comandi CLI, deploy, runbook, stima dei
costi, readiness) è in inglese nel [README.md](README.md) e in `docs/`.

## Licenza

[MIT](LICENSE).
