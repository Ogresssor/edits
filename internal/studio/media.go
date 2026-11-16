package studio

import (
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"
)

type Media struct {
	Binary  string
	Context context.Context
}
type Info struct {
	Duration float64 `json:"duration"`
	Width    int     `json:"width"`
	Height   int     `json:"height"`
	FPS      float64 `json:"fps"`
	Audio    bool    `json:"has_audio"`
}

func FindFFmpeg() (string, error) {
	if p := os.Getenv("EDITS_FFMPEG"); p != "" {
		if _, e := os.Stat(p); e != nil {
			return "", e
		}
		return filepath.Abs(p)
	}
	if p, e := exec.LookPath("ffmpeg"); e == nil {
		return p, nil
	}
	exe, _ := os.Executable()
	for _, p := range []string{filepath.Join(filepath.Dir(exe), "ffmpeg.exe"), filepath.Join(".local", "bin", "ffmpeg.exe")} {
		if st, e := os.Stat(p); e == nil && !st.IsDir() {
			return filepath.Abs(p)
		}
	}
	return "", errors.New("FFmpeg not found; set EDITS_FFMPEG")
}
func (m Media) Run(dir string, timeout time.Duration, args ...string) (string, error) {
	base := m.Context
	if base == nil {
		base = context.Background()
	}
	ctx, cancel := context.WithTimeout(base, timeout)
	defer cancel()
	cmd := exec.CommandContext(ctx, m.Binary, append([]string{"-hide_banner", "-nostdin"}, args...)...)
	cmd.Dir = dir
	b, e := cmd.CombinedOutput()
	log := string(b)
	if e != nil {
		tail := log
		if len(tail) > 2500 {
			tail = tail[len(tail)-2500:]
		}
		return log, fmt.Errorf("FFmpeg: %w: %s", e, tail)
	}
	return log, nil
}

var durationRE = regexp.MustCompile(`Duration: (\d+):(\d+):(\d+\.\d+)`)
var videoRE = regexp.MustCompile(`Video:.*? (\d{2,5})x(\d{2,5}).*? ([\d.]+) fps`)

func ParseInfo(log string) (Info, error) {
	var out Info
	d := durationRE.FindStringSubmatch(log)
	v := videoRE.FindStringSubmatch(log)
	if len(d) != 4 || len(v) != 4 {
		return out, errors.New("cannot parse video metadata")
	}
	h, _ := strconv.Atoi(d[1])
	min, _ := strconv.Atoi(d[2])
	sec, _ := strconv.ParseFloat(d[3], 64)
	out.Duration = float64(h*3600+min*60) + sec
	out.Width, _ = strconv.Atoi(v[1])
	out.Height, _ = strconv.Atoi(v[2])
	out.FPS, _ = strconv.ParseFloat(v[3], 64)
	out.Audio = strings.Contains(log, "Audio:")
	if out.FPS <= 0 {
		return out, errors.New("invalid fps")
	}
	return out, nil
}
func (m Media) Probe(path string) (Info, error) {
	log, e := m.Run("", 30*time.Second, "-i", path)
	if m.Context != nil && m.Context.Err() != nil {
		return Info{}, m.Context.Err()
	}
	info, parseErr := ParseInfo(log)
	if parseErr != nil && e != nil {
		return info, e
	}
	return info, parseErr
}

type SceneMap struct {
	SHA        string    `json:"source_sha256"`
	Duration   float64   `json:"duration"`
	Method     string    `json:"method"`
	Complete   bool      `json:"complete_technical_scan"`
	Human      bool      `json:"continuous_human_review"`
	Transcript bool      `json:"full_transcript"`
	Scenes     []Segment `json:"scenes"`
}

func (m SceneMap) Validate(sha string, duration float64) error {
	if !m.Complete || m.SHA != sha {
		return errors.New("full source scene map required")
	}
	previous := 0.0
	for _, s := range m.Scenes {
		if !finite(s.Start) || !finite(s.End) || abs(s.Start-previous) > .02 || s.End <= s.Start {
			return errors.New("scene map gap or overlap")
		}
		previous = s.End
	}
	if abs(previous-duration) > .15 {
		return errors.New("scene map does not cover source")
	}
	return nil
}
func abs(n float64) float64 {
	if n < 0 {
		return -n
	}
	return n
}
func Analyze(m Media, s *Store, path string) (string, error) {
	sha, e := s.SourceHash(path)
	if e != nil {
		return "", e
	}
	target := filepath.Join(s.Root, "analysis", sha+".json")
	var cached SceneMap
	info, e := m.Probe(path)
	if e != nil {
		return "", e
	}
	if ReadJSON(target, &cached) == nil && cached.Validate(sha, info.Duration) == nil {
		return target, nil
	}
	log, e := m.Run("", 4*time.Hour, "-i", path, "-an", "-vf", "scale=320:-2,select='gt(scene,0.35)',showinfo", "-f", "null", "-")
	if e != nil {
		return "", e
	}
	times := []float64{0}
	re := regexp.MustCompile(`pts_time:([\d.]+)`)
	for _, x := range re.FindAllStringSubmatch(log, -1) {
		t, _ := strconv.ParseFloat(x[1], 64)
		if t > 0 && t < info.Duration {
			times = append(times, t)
		}
	}
	sort.Float64s(times)
	times = append(times, info.Duration)
	sceneMap := SceneMap{SHA: sha, Duration: info.Duration, Method: "full-duration FFmpeg scene detection", Complete: true}
	for i := 1; i < len(times); i++ {
		if times[i] > times[i-1] {
			sceneMap.Scenes = append(sceneMap.Scenes, Segment{times[i-1], times[i]})
		}
	}
	if e = sceneMap.Validate(sha, info.Duration); e != nil {
		return "", e
	}
	if e = WriteJSON(target, sceneMap); e != nil {
		return "", e
	}
	return target, AtomicWrite(strings.TrimSuffix(target, ".json")+".log", []byte(log))
}
