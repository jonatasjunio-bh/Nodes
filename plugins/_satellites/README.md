# Satélites — todos os satélites ativos num globo 3D no HUD

Plugin do NODES que mostra **onde está cada satélite ativo agora** (~16 mil), girando ao vivo em volta de um globo 3D, na tela web do HUD (a mesma do mapa de câmeras).

---

## Como usar

### Comandos de voz

| Você diz | Ação | O que acontece |
|---|---|---|
| "abre o globo de satélites" | `open` | Abre o globo no HUD |
| "onde está a ISS agora?" | `where` | Diz posição, altitude e velocidade, e abre o globo seguindo a ISS |
| "quando a ISS passa aqui?" | `passes` | Próximas passagens sobre a sua cidade (48 h, acima de 10°) |
| "quem está no espaço agora?" | `crew` | Tripulação da ISS e da Tiangong |
| "mostra a ISS ao vivo" | `live` | Vídeo ao vivo da estação na janela de vídeo do HUD |
| "mostra a imagem de satélite do Brasil agora" / "a Terra inteira" | `earth` | Foto atual do GOES-19 (ou GOES-18) como cartão no HUD |
| "imagens do Hubble" | `hubble` | Uma imagem recente do Hubble no HUD, e as outras listadas |
| "quantos satélites tem acima de mim?" | `count` | Totais por categoria e quantos estão acima da sua cidade |
| "fecha o globo" | `close` | Fecha |

"Sua cidade" é a cidade do resumo do dia (hoje, Rio de Janeiro).

### No globo

- **Arrastar** gira, a **roda do mouse** aproxima. A Terra é iluminada pelo Sol de verdade: o lado da noite mostra as luzes das cidades.
- **Passar o mouse** num satélite mostra o nome; **clicar** seleciona.
- **Selecionado:** posição, altitude, velocidade, tempo de uma volta, inclinação, perigeu e apogeu, ano de lançamento, número NORAD, a **órbita desenhada** e as **próximas passagens** sobre a sua cidade. **🎯 Seguir** mantém a câmera atrás dele.
- **Categorias** (cor · total · quantos estão acima de você agora): clicar liga/desliga a categoria no globo; **☰** lista todos os satélites dela (500 por vez).
- **Procurar:** nome ou número NORAD.
- **Esc:** fecha a janela aberta, ou tira a seleção.

### Satélites com interação

| Satélite | O que dá para fazer |
|---|---|
| **ISS** | ▶ vídeo ao vivo no HUD · 👩‍🚀 tripulação · 🕑 passagens |
| **Tiangong** (estação chinesa) | 👩‍🚀 tripulação · 🕑 passagens |
| **Hubble** | 🔭 imagens recentes (clicar abre grande; "Mostrar no HUD") · 🕑 passagens |
| **GOES-19** (vê o Brasil) | 🌎 a Terra agora: América do Sul (sul e norte) e Terra inteira; "Mostrar no HUD" |
| **GOES-18** (vê o Pacífico) | 🌎 a Terra inteira agora |

Todos os outros têm informação, órbita, seguir e passagens.

---

## De onde vêm os dados (nenhuma chave)

| Fonte | Para quê |
|---|---|
| CelesTrak (GP/TLE, grupo `active`) | Elementos de órbita de todos os satélites ativos |
| CelesTrak (grupos stations, gnss, weather, science, geodetic, resource, planet, spire, geo, amateur, education, military) | Separar por categoria; Starlink, outras constelações e alguns outros são reconhecidos pelo nome |
| satellite.js 5.0.0 (página, num worker) e sgp4 (Python) | Calcular as posições (SGP4) |
| three.js 0.160 + texturas do three-globe | O globo |
| corquaid people-in-space (open-notify como reserva) | Tripulação. A open-notify ainda listava astronautas que voltaram em 2025 |
| NOAA STAR (CDN do GOES) | Fotos atuais da Terra, a cada 10 min |
| ESA/Hubble (feeds de imagens) | Imagens recentes do Hubble. A biblioteca da NASA trazia fotos do lançamento do telescópio Roman |
| YouTube (via world_cams) | Vídeo ao vivo da ISS |

A CelesTrak pede que cada arquivo seja baixado no máximo a cada 2 horas: os satélites ficam guardados 6 h (`memory/sats_active.tle`) e as categorias 24 h (`memory/sats_groups.json`). Ao abrir o NODES, o plugin carrega isso em segundo plano.

O servidor local segue o mesmo padrão do mapa de câmeras: só `127.0.0.1`, porta aleatória e um código por sessão em toda chamada `/api/*`.

---

## Arquivos

```
plugins/
├── satellites.py              ← o plugin (voz, cálculos, servidor local)
└── _satellites/
    ├── globe.html             ← o globo
    └── README.md              ← este documento
memory/
├── sats_active.tle            ← elementos de órbita (6 h)
└── sats_groups.json           ← categorias (24 h)
```

Dependência nova: `sgp4` (no `requirements.txt`). O globo usa a tela web do HUD, que precisa do `PyQt6-WebEngine` (já instalado para o mapa de câmeras).

---

## Limitações

- **Só satélites ativos.** Detritos e estágios de foguete (dezenas de milhares) ficam de fora.
- **Precisão:** as posições vêm de elementos de órbita de até algumas horas atrás; para satélites que manobram (Starlink) o erro pode chegar a alguns quilômetros.
- **Passagens:** são geométricas (acima de 10° no céu). Ver a olho nu exige o satélite iluminado pelo Sol com o céu escuro, ou seja, no começo da noite ou antes do amanhecer.
- **"Acima de você"** conta tudo acima do horizonte, inclusive os geoestacionários, que estão sempre lá.
- **Internet obrigatória** para texturas, bibliotecas e dados.

---

## O que foi testado

| Teste | Resultado |
|---|---|
| Carregar 15.894 satélites e separar por categoria | ✅ |
| ISS: posição, passagens sobre o Rio (Python e página batem), tripulação | ✅ |
| Globo no navegador: textura alinhada (América do Sul sob o GOES-19), dia/noite, seleção, órbita, seguir, listas | ✅ |
| Foto do GOES-19 (Terra inteira) na janela do globo | ✅ |
| **HUD real**: globo com WebGL, vídeo ao vivo da ISS na janela de vídeo, foto do GOES-19 como cartão | ✅ |
| Comandos por voz com o assistente de verdade | ❌ Não testado |
