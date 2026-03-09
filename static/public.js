window.PageZapboxPublic = {
  template: '#page-zapbox-public',
  data() {
    return {
      zapbox: null,
      url: '',
      lnurl: '',
      activeUrl: '',
      activeSwitch: null
    }
  },
  watch: {
    activeSwitch(val) {
      this.activeUrl = `${this.url}?pin=${val.pin}`
    }
  },
  created() {
    const bsId = this.$route.params.id
    this.url = `${window.location.origin}/zapbox/api/v1/lnurl/${bsId}`
    LNbits.api
      .request('GET', `/zapbox/api/v1/public/${bsId}`)
      .catch(LNbits.utils.notifyApiError)
      .then(res => {
        this.zapbox = res.data
        this.activeSwitch = this.zapbox.switches[0]
        this.activeUrl = `${this.url}?pin=${this.activeSwitch.pin}`
      })
  }
}
