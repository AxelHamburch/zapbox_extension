window.PageZapbox = {
  template: '#page-zapbox',
  data() {
    return {
      url: window.location.origin + '/zapbox/api/v1/lnurl',
      apiUrl: window.location.origin + '/zapbox/api/v1',
      activeUrl: 0,
      activePin: 0,
      lnurl: '',
      filter: '',
      currency: 'sat',
      zapboxes: [],
      authKeys: [],
      nfcIdentities: [],
      zapboxTable: {
        columns: [
          {
            name: 'title',
            align: 'left',
            label: 'title',
            field: 'title'
          },
          {
            name: 'wallet',
            align: 'left',
            label: 'wallet',
            field: 'wallet'
          },
          {
            name: 'currency',
            align: 'left',
            label: 'currency',
            field: 'currency'
          },
          {
            name: 'disabled',
            align: 'left',
            label: 'disabled',
            field: 'disabled'
          },
          {
            name: 'disposable',
            align: 'left',
            label: 'disposable',
            field: 'disposable'
          }
        ],
        pagination: {
          rowsPerPage: 10
        }
      },
      qrCodeDialog: {
        show: false,
        data: null
      },
      formDialog: {
        show: false,
        data: {
          switches: [],
          lnurl_toggle: false,
          show_message: false,
          show_ack: false,
          show_price: 'None',
          device: 'pos',
          profit: 1,
          amount: 1,
          title: '',
          disabled: false,
          disposable: true
        }
      }
    }
  },
  computed: {
    currencies() {
      if (this.g.allowedCurrencies.length > 0) {
        return ['sat', ...this.g.allowedCurrencies]
      } else {
        return ['sat', ...this.g.currencies]
      }
    }
  },
  watch: {
    activePin() {
      this.generateSwitchUrl()
    },
    tab() {
      this.generateSwitchUrl()
    }
  },
  methods: {
    clearFormDialog() {
      this.formDialog.data = {
        switches: [],
        lnurl_toggle: false,
        show_message: false,
        show_ack: false,
        show_price: 'None',
        device: 'pos',
        profit: 1,
        amount: 1,
        title: '',
        disabled: false,
        disposable: true,
        teach_pin: '',
        touch_enabled: true,
        auth_enabled: true,
        tagid_base_url: null,
        tagid_api_key: null
      }
      this.authKeys = []
      this.nfcIdentities = []
    },
    openPublicLink(id) {
      window.open(`/zapbox/public/${id}`, '_blank')
    },
    switchLabel(_switch) {
      const label = _switch.label !== null ? _switch.label : 'Switch '
      return label + ' pin: ' + _switch.pin + ' (' + _switch.duration + ' ms)'
    },
    generateSwitchUrl() {
      const _switch = this.qrCodeDialog.data.switches.find(
        s => s.pin === this.activePin
      )
      this.activeUrl = `${this.url}/${this.qrCodeDialog.data.id}?pin=${_switch.pin}`
    },
    openQrCodeDialog(zapboxId) {
      const zapbox = _.findWhere(this.zapboxes, {
        id: zapboxId
      })
      this.qrCodeDialog.data = _.clone(zapbox)
      this.activePin = zapbox.switches[0].pin
      this.qrCodeDialog.show = true
    },
    addSwitch() {
      this.formDialog.data.switches.push({
        amount: 10,
        pin: 0,
        duration: 1000,
        variable: false,
        comment: false
      })
    },
    removeSwitch() {
      this.formDialog.data.switches.pop()
    },
    cancelFormDialog() {
      this.formDialog.show = false
      this.clearFormDialog()
    },
    closeFormDialog() {
      this.clearFormDialog()
      this.formDialog.show = false
    },
    sendFormData() {
      if (this.formDialog.data.id) {
        this.updateZapBox()
      } else {
        this.createZapBox()
      }
    },

    createZapBox() {
      LNbits.api
        .request('POST', this.apiUrl, null, this.formDialog.data)
        .then(response => {
          this.zapboxes.push(response.data)
          this.closeFormDialog()
        })
        .catch(error => {
          LNbits.utils.notifyApiError(error)
        })
    },
    updateZapBox() {
      LNbits.api
        .request(
          'PUT',
          this.apiUrl + '/' + this.formDialog.data.id,
          null,
          this.formDialog.data
        )
        .then(response => {
          const index = this.zapboxes.findIndex(
            obj => obj.id === response.data.id
          )
          this.zapboxes[index] = response.data
          this.$q.notify({
            type: 'success',
            message: 'ZapBox updated successfully!'
          })
          this.closeFormDialog()
        })
        .catch(LNbits.utils.notifyApiError)
    },
    getZapBoxes() {
      LNbits.api
        .request('GET', this.apiUrl)
        .then(response => {
          if (response.data.length > 0) {
            this.zapboxes = response.data
          }
        })
        .catch(LNbits.utils.notifyApiError)
    },
    deleteZapBox(zapboxId) {
      LNbits.utils
        .confirmDialog('Are you sure you want to delete this pay link?')
        .onOk(() => {
          LNbits.api
            .request('DELETE', this.apiUrl + '/' + zapboxId)
            .then(() => {
              this.zapboxes = _.reject(
                this.zapboxes,
                obj => obj.id === zapboxId
              )
            })
            .catch(LNbits.utils.notifyApiError)
        })
    },
    triggerPin() {
      const _id = this.qrCodeDialog.data.id
      LNbits.api
        .request('PUT', `${this.apiUrl}/trigger/${_id}/${this.activePin}`)
        .then(() => {
          this.$q.notify({
            type: 'positive',
            message: 'Switch triggered successfully!'
          })
        })
        .catch(LNbits.utils.notifyApiError)
    },
    openUpdateZapBox(zapboxId) {
      const zapbox = _.findWhere(this.zapboxes, {
        id: zapboxId
      })
      this.formDialog.data = _.clone(zapbox)
      this.getAuthKeys(zapboxId)
      this.getNfcIdentities(zapboxId)
      this.formDialog.show = true
    },
    getNfcIdentities(zapboxId) {
      if (!zapboxId) { this.nfcIdentities = []; return }
      LNbits.api
        .request('GET', `${this.apiUrl}/nfc/identities/${zapboxId}`)
        .then(response => { this.nfcIdentities = response.data })
        .catch(LNbits.utils.notifyApiError)
    },
    updateNfcIdentity(nfc) {
      LNbits.api
        .request('PUT', `${this.apiUrl}/nfc/identities/${nfc.id}`, null, {
          label: nfc.label,
          enabled: nfc.enabled
        })
        .then(() => {
          this.$q.notify({ type: 'positive', message: 'NFC card updated.' })
        })
        .catch(LNbits.utils.notifyApiError)
    },
    deleteNfcIdentity(nfc) {
      LNbits.utils.confirmDialog('Remove this NFC card?').onOk(() => {
        LNbits.api
          .request('DELETE', `${this.apiUrl}/nfc/identities/${nfc.id}`)
          .then(() => {
            this.nfcIdentities = _.reject(this.nfcIdentities, obj => obj.id === nfc.id)
          })
          .catch(LNbits.utils.notifyApiError)
      })
    },
    shortKey(pubkey) {
      if (!pubkey) return ''
      return pubkey.length > 12
        ? `${pubkey.slice(0, 6)}…${pubkey.slice(-4)}`
        : pubkey
    },
    formatDate(value) {
      if (!value) return ''
      return new Date(value).toLocaleString()
    },
    getAuthKeys(zapboxId) {
      if (!zapboxId) {
        this.authKeys = []
        return
      }
      LNbits.api
        .request('GET', `${this.apiUrl}/auth/keys/${zapboxId}`)
        .then(response => {
          this.authKeys = response.data
        })
        .catch(LNbits.utils.notifyApiError)
    },
    updateAuthKey(authKey) {
      LNbits.api
        .request('PUT', `${this.apiUrl}/auth/keys/${authKey.id}`, null, {
          label: authKey.label,
          enabled: authKey.enabled
        })
        .then(() => {
          this.$q.notify({
            type: 'positive',
            message: 'Identity updated.'
          })
        })
        .catch(LNbits.utils.notifyApiError)
    },
    deleteAuthKey(authKey) {
      LNbits.utils
        .confirmDialog('Remove this identity?')
        .onOk(() => {
          LNbits.api
            .request('DELETE', `${this.apiUrl}/auth/keys/${authKey.id}`)
            .then(() => {
              this.authKeys = _.reject(
                this.authKeys,
                obj => obj.id === authKey.id
              )
            })
            .catch(LNbits.utils.notifyApiError)
        })
    },
    copyDeviceString(zapboxId) {
      const loc = `wss://${window.location.host}/api/v1/ws/${zapboxId}`
      this.utils.copyText(loc, 'Device string copied to clipboard!')
    },
    exportCSV() {
      LNbits.utils.exportCSV(
        this.zapboxTable.columns,
        this.zapboxes
      )
    }
  },
  created() {
    this.getZapBoxes()
  }
}
